"""Deterministic, physics-inspired synthetic telemetry with separate ground truth.

THIS IS NOT A VALIDATED ENGINE MODEL. Every constant is an assumption chosen to
be plausible for a liquid-cooled passenger-car engine (thermostat-regulated
coolant, warm-up from ambient, load- and heat-dependent rise, idle fan cycling,
1 C sensor quantisation, autocorrelated noise). The benchmark measures detectors
against these assumptions *and* against deliberate violations of them. Results
on this data say nothing about real-world accuracy.

Design rules
- Telemetry rows contain only the CSV schema columns. Ground truth (labels,
  onsets, magnitudes, each row's true instant) is returned in a separate
  ``GroundTruth`` object that only the evaluator may read.
- Independent random streams are derived with SHA-256 from (seed, vehicle,
  purpose). Physics and driving use streams keyed by (seed, vehicle) only, so a
  vehicle's base trajectory is identical in every scenario (paired design), and
  injections draw from a separate stream: enabling a fault cannot change any
  sample before its onset (tested in tests/test_synth.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence

from .ingest import CANONICAL_COLUMNS

Row = tuple[str, str, str, str, str, str]
Category = Literal["anomaly", "sensor_fault", "nuisance"]

US = 1_000_000
DAY_US = 86_400 * US
EPOCH0 = datetime(2026, 1, 5, tzinfo=timezone.utc)   # a Monday; arbitrary
EPOCH0_US = int(EPOCH0.timestamp()) * US
REGIMES = ("idle", "urban", "highway")
_NOISE_SIGMA = {"idle": 0.9, "urban": 0.6, "highway": 0.4}
_BASE_LOAD = {"idle": 12.0, "urban": 28.0, "highway": 38.0}
_OFFSETS = (timezone.utc, timezone(timedelta(hours=2)), timezone(timedelta(hours=-3)),
            timezone(timedelta(hours=5, minutes=30)))


# ---------------------------------------------------------------- truth types

@dataclass(frozen=True)
class Event:
    """A ground-truth interval. Categories drive scoring:
    anomaly       should be detected (recall, delay)
    sensor_fault  should not be reported as 'normal' (false reassurance)
    nuisance      a legitimate or data-quality condition that must not alarm
    """

    vehicle_id: str
    kind: str
    category: Category
    onset_us: int
    end_us: int
    params: tuple[tuple[str, float], ...] = ()

    def param(self, name: str) -> float:
        return dict(self.params)[name]


@dataclass(frozen=True)
class GroundTruth:
    events: tuple[Event, ...]
    observation_vehicles: tuple[str, ...]   # true vehicle of each observation row
    observation_epochs_us: tuple[int, ...]  # true instant of each observation row
    vehicles_without_reference: frozenset[str]
    eval_start_us: int
    eval_end_us: int


@dataclass(frozen=True)
class Dataset:
    scenario: str
    seed: int
    reference_rows: tuple[Row, ...]
    observation_rows: tuple[Row, ...]
    truth: GroundTruth


# ------------------------------------------------------------ vehicle physics

@dataclass(frozen=True)
class VehicleParams:
    t_set: float          # thermostat-regulated coolant temperature, C
    load_gain: float      # C per % engine load above 50 %
    ambient_gain: float   # C per C ambient above 30 C
    heat_rate: float      # warm-up heating rate, C/s at moderate load
    cool_tau_s: float     # parked cooling time constant, s
    noise_scale: float    # multiplier on regime noise sigma
    fan_amp: float        # idle fan-cycling amplitude, C
    fan_period_s: float
    idle_rpm: float
    gear_urban: float     # rpm per km/h
    gear_highway: float
    climate_c: float      # mean ambient of the vehicle's home climate


def _rng(seed: int, *parts: object) -> random.Random:
    digest = hashlib.sha256(":".join(str(p) for p in (seed, *parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _params(seed: int, k: int) -> VehicleParams:
    r = _rng(seed, k, "params")
    return VehicleParams(
        t_set=r.uniform(86, 95), load_gain=r.uniform(0.12, 0.22),
        ambient_gain=r.uniform(0.15, 0.30), heat_rate=r.uniform(0.12, 0.18),
        cool_tau_s=r.uniform(3000, 6000), noise_scale=r.uniform(0.8, 1.25),
        fan_amp=r.uniform(0.6, 1.6), fan_period_s=r.uniform(150, 300),
        idle_rpm=r.uniform(700, 850), gear_urban=r.uniform(26, 34),
        gear_highway=r.uniform(14, 19), climate_c=r.uniform(12, 24))


@dataclass(frozen=True)
class Trip:
    start_us: int
    duration_s: float


def _schedule(r: random.Random, days: int) -> list[Trip]:
    """1-3 trips per day in disjoint morning/midday/evening slots (UTC)."""
    slots = ((6.5, 9.0), (11.5, 14.0), (16.5, 19.5))
    trips = []
    for day in range(days):
        count = r.choice((1, 2, 2, 3))
        for slot in sorted(r.sample(range(3), count)):
            start_h = r.uniform(*slots[slot])
            trips.append(Trip(EPOCH0_US + day * DAY_US + int(start_h * 3600) * US, r.uniform(15, 55) * 60))
    return trips


@dataclass
class Setup:
    """Scenario-specific modifications of one vehicle (ground truth side)."""

    has_reference: bool = True
    reference_no_highway: bool = False
    overheat: tuple[int, float] | None = None          # (onset_us, delta C at idle)
    thermostat_open: tuple[int, float] | None = None   # (onset_us, delta C at speed)
    drift: tuple[int, float] | None = None             # (onset_us, signed C/day)
    stuck: int | None = None                           # onset_us
    payload: tuple[int, float] | None = None           # (onset_us, extra load %)
    eval_ambient_shift_c: float = 0.0
    unreliable: bool = False
    integrity_noise: bool = False


@dataclass(frozen=True)
class Options:
    reference_days: int = 10
    eval_days: int = 20
    vehicles: int = 8
    dt_s: float = 30.0
    noise_multiplier: float = 1.0


@dataclass
class _Sample:
    epoch_us: int
    rpm: float
    speed: float
    load: float
    coolant: str  # formatted measurement ("" when missing)


def _simulate(seed: int, k: int, setup: Setup, trips: list[Trip], drive: random.Random,
              fault: random.Random, opts: Options, eval_start_us: int) -> list[_Sample]:
    p = _params(seed, k)
    noise = _rng(seed, k, "noise")
    out: list[_Sample] = []
    temp = p.climate_c
    last_end_us = None
    day_ambient: dict[int, float] = {}
    stuck_value: str | None = None
    dt = opts.dt_s
    phi = math.exp(-dt / 120.0)

    for trip in trips:
        day = (trip.start_us - EPOCH0_US) // DAY_US
        if day not in day_ambient:
            day_ambient[day] = drive.gauss(0.0, 2.0)
        if last_end_us is not None:
            hour = (trip.start_us % DAY_US) / 3_600_000_000
            ambient0 = _ambient(p, day_ambient[day], hour, trip.start_us, setup, eval_start_us)
            parked_s = (trip.start_us - last_end_us) / US
            temp = ambient0 + (temp - ambient0) * math.exp(-parked_s / p.cool_tau_s)
        else:
            temp = _ambient(p, day_ambient[day], 0.0, trip.start_us, setup, eval_start_us)
        regime, dwell_left, target = "idle", drive.uniform(30, 120), 0.0
        speed, hill_left, hill_load = 0.0, 0.0, 0.0
        err = noise.gauss(0.0, 1.0) * _NOISE_SIGMA["idle"] * p.noise_scale * opts.noise_multiplier
        steps = int(trip.duration_s // dt)
        now_us = trip.start_us
        for _ in range(steps):
            # --- driving (always the same number of draws per step: alignment)
            u_regime, u_target, g_speed, g_rpm, g_load, u_hill, u_hill_len = (
                drive.random(), drive.random(), drive.gauss(0, 1), drive.gauss(0, 1),
                drive.gauss(0, 1), drive.random(), drive.random())
            jitter_s = drive.randint(-3, 3)
            dwell_left -= dt
            if dwell_left <= 0:
                regime = _next_regime(regime, u_regime)
                if regime == "highway" and setup.reference_no_highway and now_us < eval_start_us:
                    regime = "urban"
                dwell_left = {"idle": 30 + 150 * u_target, "urban": 120 + 480 * u_target,
                              "highway": 240 + 960 * u_target}[regime]
                target = {"idle": 0.0, "urban": 20 + 35 * u_target, "highway": 85 + 35 * u_target}[regime]
            previous_speed = speed
            speed = max(0.0, speed + (target - speed) * 0.5 + (1.5 * g_speed if regime != "idle" else 0.0))
            if regime == "idle":
                speed *= 0.3
            accel = (speed - previous_speed) / dt
            if speed < 3:
                rpm = p.idle_rpm + 20 * g_rpm
            else:
                rpm = p.idle_rpm + speed * (p.gear_highway if regime == "highway" else p.gear_urban) + 60 * g_rpm
            rpm = min(4500.0, max(600.0, rpm))
            if hill_left <= 0 and u_hill < 0.01:
                hill_left, hill_load = 60 + 180 * u_hill_len, 10 + 20 * u_hill_len
            hill_left -= dt
            payload = setup.payload[1] if setup.payload and now_us >= setup.payload[0] else 0.0
            load = (_BASE_LOAD[regime] + 25 * min(1.0, max(0.0, accel)) + (hill_load if hill_left > 0 else 0.0)
                    + payload + 4 * g_load)
            load = min(100.0, max(0.0, load))
            hour = (now_us % DAY_US) / 3_600_000_000
            ambient = _ambient(p, day_ambient[day], hour, now_us, setup, eval_start_us)

            # --- thermal model (true coolant temperature)
            speed_factor = min(1.0, speed / 100.0)
            target_temp = p.t_set + p.load_gain * max(0.0, load - 50) + p.ambient_gain * max(0.0, ambient - 30)
            if setup.overheat and now_us >= setup.overheat[0]:
                ramp = 1 - math.exp(-(now_us - setup.overheat[0]) / (600 * US))
                target_temp += setup.overheat[1] * ramp * (1 - 0.6 * speed_factor)
            open_stuck = bool(setup.thermostat_open and now_us >= setup.thermostat_open[0])
            if open_stuck and setup.thermostat_open:
                target_temp -= setup.thermostat_open[1] * (0.4 + 0.6 * speed_factor)
            heater = 0.03 if ambient < 5 else 0.0  # cabin heater draws heat in the cold
            if temp < min(p.t_set, target_temp) - 1:
                heat = p.heat_rate * (0.6 + load / 75) * (0.6 if open_stuck else 1.0)
                loss = (2e-4 * (2.0 if open_stuck else 1.0) * (temp - ambient) * (1 + speed / 80)) + heater
                temp = min(temp + (heat - loss) * dt, target_temp + 2.0)  # small thermostat-opening overshoot
            else:
                temp += (target_temp - temp) * (1 - math.exp(-dt / 90.0))

            # --- sensor (measured, quantised)
            sigma = _NOISE_SIGMA[regime] * p.noise_scale * opts.noise_multiplier
            err = phi * err + math.sqrt(1 - phi * phi) * sigma * noise.gauss(0.0, 1.0)
            fan = (p.fan_amp * math.sin(2 * math.pi * (now_us / US) / p.fan_period_s)
                   if regime == "idle" and temp >= p.t_set - 1 else 0.0)
            measured = temp + err + fan
            if setup.drift and now_us >= setup.drift[0]:
                measured += setup.drift[1] * (now_us - setup.drift[0]) / DAY_US
            coolant = str(int(round(measured)))
            if setup.stuck is not None and now_us >= setup.stuck:
                if stuck_value is None:
                    stuck_value = coolant
                coolant = stuck_value
            in_eval = now_us >= eval_start_us
            if setup.unreliable and in_eval:
                u = fault.random()
                if u < 0.004:
                    coolant = fault.choice(("-40", "215", str(int(round(measured + fault.uniform(15, 35))))))
                elif u < 0.054:
                    coolant = ""
            out.append(_Sample(now_us, rpm, speed, load, coolant))
            now_us += int((dt + jitter_s) * US)
        last_end_us = now_us
    return out


def _ambient(p: VehicleParams, day_noise: float, hour: float, now_us: int, setup: Setup,
             eval_start_us: int) -> float:
    shift = setup.eval_ambient_shift_c if now_us >= eval_start_us else 0.0
    return p.climate_c + 6 * math.sin(2 * math.pi * (hour - 9) / 24) + day_noise + shift


def _next_regime(regime: str, u: float) -> str:
    if regime in ("idle", "highway"):
        return "urban"
    return "idle" if u < 0.35 else ("highway" if u < 0.65 else "urban")


# --------------------------------------------------------------- scenarios

def _onset_in_trip(trips: list[Trip], r: random.Random, start_us: int, end_us: int) -> int:
    """A random instant inside a random trip that starts in [start_us, end_us)."""
    candidates = [t for t in trips if start_us <= t.start_us < end_us]
    trip = r.choice(candidates)
    return trip.start_us + int(r.uniform(0.2, 0.8) * trip.duration_s * US)


Spec = tuple[str, Category, int, dict[str, float]]   # (kind, category, onset_us, params)
Built = tuple[Setup, list[Spec]]
Builder = Callable[[int, list[Trip], random.Random, int, int], Built]


def _normal(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    return Setup(), []


def _abrupt(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    if k <= 2:
        onset, delta = _onset_in_trip(trips, r, s + 2 * DAY_US, s + 12 * DAY_US), r.uniform(12, 22)
        return Setup(overheat=(onset, delta)), [("overheat", "anomaly", onset, {"delta_c": delta})]
    if k <= 4:
        onset, delta = _onset_in_trip(trips, r, s + 2 * DAY_US, s + 12 * DAY_US), r.uniform(12, 20)
        return Setup(thermostat_open=(onset, delta)), [("thermostat_stuck_open", "anomaly", onset, {"delta_c": delta})]
    return Setup(), []


def _drift(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    if k <= 4:
        onset = s + int(r.uniform(0, 3) * DAY_US)
        rate = r.uniform(0.3, 1.0) * (1 if k % 2 == 0 else -1)
        return Setup(drift=(onset, rate)), [("sensor_drift", "anomaly", onset, {"rate_c_per_day": rate})]
    return Setup(), []


def _load(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    if k <= 4:
        onset, extra = s + int(r.uniform(2, 6) * DAY_US), r.uniform(20, 35)
        return Setup(payload=(onset, extra)), [("load_increase", "nuisance", onset, {"extra_load_pct": extra})]
    return Setup(), []


def _seasonal(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    shift = r.uniform(15, 20) if k <= 3 else -r.uniform(25, 30)
    kind = "hot_season" if shift > 0 else "cold_season"
    return Setup(eval_ambient_shift_c=shift), [(kind, "nuisance", s, {"ambient_shift_c": shift})]


def _unreliable(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    events: list[Spec] = [("unreliable_sensor", "nuisance", s, {})]
    if k <= 2:
        onset = _onset_in_trip(trips, r, s + 1 * DAY_US, s + 10 * DAY_US)
        events.append(("sensor_stuck", "sensor_fault", onset, {}))
        return Setup(unreliable=True, stuck=onset), events
    return Setup(unreliable=True), events


def _unseen(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    if k <= 2:
        setup = Setup(has_reference=False)
        events: list[Spec] = [("no_reference", "nuisance", s, {})]
        if k == 0:
            onset, delta = _onset_in_trip(trips, r, s + 2 * DAY_US, s + 12 * DAY_US), r.uniform(15, 22)
            setup.overheat = (onset, delta)
            events.append(("overheat", "anomaly", onset, {"delta_c": delta}))
        return setup, events
    if k <= 5:
        setup = Setup(reference_no_highway=True)
        events = [("unseen_regime", "nuisance", s, {})]
        if k == 4:
            onset, delta = _onset_in_trip(trips, r, s + 2 * DAY_US, s + 12 * DAY_US), r.uniform(15, 22)
            setup.overheat = (onset, delta)
            events.append(("overheat", "anomaly", onset, {"delta_c": delta}))
        return setup, events
    return Setup(), []


def _integrity(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    events: list[Spec] = [("integrity_noise", "nuisance", s, {})]
    if k <= 2:
        onset, delta = _onset_in_trip(trips, r, s + 2 * DAY_US, s + 12 * DAY_US), r.uniform(12, 22)
        events.append(("overheat", "anomaly", onset, {"delta_c": delta}))
        return Setup(integrity_noise=True, overheat=(onset, delta)), events
    return Setup(integrity_noise=True), events


def _contaminated(k: int, trips: list[Trip], r: random.Random, s: int, e: int) -> Built:
    if k <= 3:
        reference_days = (s - EPOCH0_US) // DAY_US
        start_day = int(reference_days * 0.7) if k <= 1 else 0
        onset = _onset_in_trip(trips, r, EPOCH0_US + start_day * DAY_US, EPOCH0_US + (start_day + 1) * DAY_US)
        delta = r.uniform(15, 22)
        share = "partial" if k <= 1 else "full"
        return (Setup(overheat=(onset, delta)),
                [(f"overheat_in_reference_{share}", "anomaly", s, {"delta_c": delta,
                                                                    "onset_day": onset / DAY_US})])
    return Setup(), []


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    builder: Builder
    options: Options = field(default_factory=Options)


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    Scenario("normal_operation", "Negative control: multi-regime driving, cold starts, hills; no injected events.", _normal),
    Scenario("abrupt_anomaly", "v0-v2 cooling failure (overheat, strongest at low speed); v3-v4 thermostat stuck open (runs cold); v5-v7 controls.", _abrupt),
    Scenario("gradual_sensor_drift", "v0-v4 coolant sensor bias drifts 0.3-1.0 C/day (alternating sign); v5-v7 controls.", _drift),
    Scenario("load_change", "False-positive condition: v0-v4 start towing (+20-35 % load, legitimately hotter); v5-v7 controls.", _load),
    Scenario("seasonal_shift", "False-positive condition: v0-v3 hot season (+15-20 C ambient); v4-v7 severe cold (-25-30 C, longer warm-up than the detector assumes).", _seasonal),
    Scenario("unreliable_sensor", "All vehicles: 5 % missing readings, 0.4 % spikes/sentinels; v0-v2 sensor freezes.", _unreliable),
    Scenario("unseen_vehicles_and_regimes", "v0-v2 have no reference data (v0 overheats); v3-v5 reference lacks highway driving (v4 overheats).", _unseen),
    Scenario("data_integrity", "All vehicles: duplicates, conflicting duplicates, late rows, mixed UTC offsets; v0-v2 overheat.", _integrity),
    Scenario("contaminated_reference", "v0-v1 overheat starts in the last 30 % of the reference period; v2-v3 during the whole reference period.", _contaminated),
    Scenario("stress_noise_x2", "Robustness: normal operation with twice the sensor noise.", _normal, Options(noise_multiplier=2.0)),
    Scenario("stress_abrupt_noise_x2", "Robustness: abrupt anomalies with twice the sensor noise.", _abrupt, Options(noise_multiplier=2.0)),
    Scenario("stress_sampling_60s", "Robustness: abrupt anomalies sampled every 60 s instead of 30 s.", _abrupt, Options(dt_s=60.0)),
)}


# ---------------------------------------------------------------- generator

def _format_time(epoch_us: int, zone: timezone = timezone.utc) -> str:
    moment = EPOCH0 + timedelta(microseconds=epoch_us - EPOCH0_US)
    if zone is timezone.utc:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.astimezone(zone).isoformat()


def _row(vehicle: str, s: _Sample, zone: timezone = timezone.utc) -> Row:
    return (_format_time(s.epoch_us, zone), vehicle, str(int(round(s.rpm))), f"{s.speed:.1f}",
            s.coolant, str(int(round(s.load))))


def generate(scenario: str, seed: int, *, reference_days: int | None = None,
             eval_days: int | None = None, vehicles: int | None = None) -> Dataset:
    spec = SCENARIOS[scenario]
    opts = replace(spec.options,
                   **{k: v for k, v in (("reference_days", reference_days), ("eval_days", eval_days),
                                        ("vehicles", vehicles)) if v is not None})
    if opts.reference_days < 4 or opts.eval_days < 13 or opts.vehicles < 8:
        raise ValueError("scenarios need >= 4 reference days, >= 13 evaluation days and >= 8 vehicles")
    eval_start = EPOCH0_US + opts.reference_days * DAY_US
    eval_end = eval_start + opts.eval_days * DAY_US
    reference: list[Row] = []
    observed: list[tuple[int, int, int, str, int, Row]] = []  # (arrival, vehicle, position, ...)
    events: list[Event] = []
    no_reference: set[str] = set()

    for k in range(opts.vehicles):
        vehicle = f"veh-{k:02d}"
        drive = _rng(seed, k, "drive")
        fault = _rng(seed, k, "fault", scenario)
        trips = _schedule(drive, opts.reference_days + opts.eval_days)
        setup, specs = spec.builder(k, trips, fault, eval_start, eval_end)
        for kind, category, onset, params in specs:
            events.append(Event(vehicle, kind, category, onset, eval_end, tuple(sorted(params.items()))))
        if not setup.has_reference:
            no_reference.add(vehicle)
        samples = _simulate(seed, k, setup, trips, drive, fault, opts, eval_start)
        vehicle_rows: list[tuple[str, int, Row]] = []
        for s in samples:
            if s.epoch_us < eval_start:
                if setup.has_reference:
                    reference.append(_row(vehicle, s))
            else:
                zone = fault.choice(_OFFSETS) if setup.integrity_noise else timezone.utc
                vehicle_rows.append((vehicle, s.epoch_us, _row(vehicle, s, zone)))
        if setup.unreliable:
            vehicle_rows = _drop_bursts(vehicle_rows, fault)
        if setup.integrity_noise:
            vehicle_rows = _perturb_integrity(vehicle_rows, fault)
        # Arrival order: a late row arrives when the row before it in the vehicle's
        # stream arrives, so merging the fleet by arrival keeps per-vehicle order
        # (sorting by true time here would silently undo the out-of-order rows).
        arrival = 0
        for position, (v, true_us, r) in enumerate(vehicle_rows):
            arrival = max(arrival, true_us)
            observed.append((arrival, k, position, v, true_us, r))

    observed.sort(key=lambda item: item[:3])  # deterministic fleet interleaving
    truth = GroundTruth(tuple(events), tuple(item[3] for item in observed),
                        tuple(item[4] for item in observed), frozenset(no_reference), eval_start, eval_end)
    reference.sort(key=lambda r: (r[0], r[1]))
    return Dataset(scenario, seed, tuple(reference), tuple(item[5] for item in observed), truth)


def _drop_bursts(rows: list[tuple[str, int, Row]], r: random.Random) -> list[tuple[str, int, Row]]:
    """Telemetry dropouts: ~3 % of rows start a 2-15 minute burst of missing rows."""
    kept, skip_until = [], -1
    for item in rows:
        if item[1] < skip_until:
            continue
        if r.random() < 0.003:
            skip_until = item[1] + int(r.uniform(120, 900) * US)
            continue
        kept.append(item)
    return kept


def _perturb_integrity(rows: list[tuple[str, int, Row]], r: random.Random) -> list[tuple[str, int, Row]]:
    """Exact duplicates (2 %), conflicting duplicates (0.3 %), late rows (1 %)."""
    out: list[tuple[str, int, Row]] = []
    late: list[tuple[int, tuple[str, int, Row]]] = []
    for index, item in enumerate(rows):
        u = r.random()
        if u < 0.01:
            late.append((index + r.randint(2, 6), item))   # delivered a few rows later
            continue
        out.append(item)
        if u < 0.03:
            out.append(item)
        elif u < 0.033 and item[2][4]:
            changed = item[2][:4] + (str(int(item[2][4]) + r.choice((-3, -2, 2, 3))),) + item[2][5:]
            out.append((item[0], item[1], changed))
        while late and late[0][0] <= index:
            out.append(late.pop(0)[1])
    out.extend(item for _, item in late)
    return out


# ---------------------------------------------------------------------- CLI

def write_dataset(dataset: Dataset, directory: str | Path) -> None:
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    header = ",".join(CANONICAL_COLUMNS) + "\n"
    for name, rows in (("reference.csv", dataset.reference_rows), ("observations.csv", dataset.observation_rows)):
        with (folder / name).open("w", encoding="utf-8", newline="") as out:
            out.write(header)
            out.writelines(",".join(r) + "\n" for r in rows)
    truth = {
        "notice": "Synthetic ground truth for offline evaluation only. Never feed it to a detector.",
        "scenario": dataset.scenario, "seed": dataset.seed,
        "eval_start_us": dataset.truth.eval_start_us, "eval_end_us": dataset.truth.eval_end_us,
        "vehicles_without_reference": sorted(dataset.truth.vehicles_without_reference),
        "events": [{"vehicle_id": e.vehicle_id, "kind": e.kind, "category": e.category,
                    "onset_us": e.onset_us, "end_us": e.end_us, "params": dict(e.params)}
                   for e in dataset.truth.events],
    }
    (folder / "truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")


def cli_synth(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink synth",
                                     description="Write one deterministic synthetic scenario to a folder.")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", required=True, help="output folder (reference.csv, observations.csv, truth.json)")
    parser.add_argument("--reference-days", type=int)
    parser.add_argument("--eval-days", type=int)
    args = parser.parse_args(list(argv))
    try:
        dataset = generate(args.scenario, args.seed, reference_days=args.reference_days, eval_days=args.eval_days)
        write_dataset(dataset, args.out)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 2
    print(json.dumps({"scenario": args.scenario, "seed": args.seed, "out": str(args.out),
                      "reference_rows": len(dataset.reference_rows),
                      "observation_rows": len(dataset.observation_rows)}))
    return 0
