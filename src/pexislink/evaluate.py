"""Offline, event-level evaluation against synthetic ground truth.

This is the only module that reads ``GroundTruth``; detection code must never
import it (enforced by tests/test_architecture.py). Detectors are scored from
per-row verdicts ``(status, direction)`` aligned 1:1 with observation rows;
direction is +1 (reading above expectation), -1 (below) or 0 (none).

Definitions (identical for every detector; revised after independent review)
- Expected direction and horizon come from the ground truth: overheat (+1) and
  stuck-open thermostat (-1) must be detected within 24 h of onset; sensor drift
  in the sign of its rate, any time before the end of evaluation; sensor faults
  accept either direction.
- Detected event: a confirmed anomaly in the expected direction within the
  horizon. Placebo recall: the same windows and directions applied to every
  fault-free control vehicle. Excess recall = recall - placebo recall. (Without
  direction and horizon the placebo saturates for any detector that alarms
  often, which made the first version of this metric uninformative.)
- Alert: within one segment (rows no more than ``segment_gap_s`` apart), a
  maximal run of confirmed rows with the same direction. Normal or suspected
  rows, or a change of direction, end it; insufficient-evidence rows neither
  end nor extend it.
- True alert: onset inside an anomaly or sensor-fault interval of the vehicle,
  in an accepted direction. Misdirected alert: inside such an interval, in the
  wrong direction. False alert: outside every such interval.
- Eligible exposure: operating time outside anomaly and sensor-fault intervals,
  where a false alert is possible. False alerts are reported per 1000 eligible
  hours and as the share of eligible trips (segments) with at least one.
- Coverage: share of rows whose status is not ``insufficient_evidence``; also
  per nuisance condition, on the rows inside that condition's interval.
- False reassurance: share of rows inside a sensor-fault interval reported
  ``normal``.
- Uncertainty: 95 % percentile intervals from a bootstrap over vehicles (the
  unit within which alerts cluster), 1000 resamples with a fixed seed. For a
  zero false-alert count the exact Poisson upper bound is also reported; it
  assumes independent alerts.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, fields
from math import ceil, exp, lgamma, log
from typing import Callable, Sequence

from .synth import DAY_US, Event, GroundTruth

US = 1_000_000
HOUR_US = 3600 * US
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261015

Verdict = tuple[str, int]  # (status, direction)


@dataclass
class VehicleTally:
    """Counts for one vehicle in one dataset: the resampling unit."""

    rows: int = 0
    assessable: int = 0
    exposure_us: int = 0
    eligible_exposure_us: int = 0
    eligible_trips: int = 0
    trips_with_false_alert: int = 0
    events: int = 0
    detected: int = 0
    missed_without_evidence: int = 0
    placebo_events: int = 0
    placebo_detected: int = 0
    alerts: int = 0
    true_alerts: int = 0
    misdirected_alerts: int = 0
    false_alerts: int = 0
    fault_rows: int = 0
    fault_rows_normal: int = 0
    unseen_rows: int = 0
    unseen_rows_normal: int = 0


TALLY_FIELDS = tuple(f.name for f in fields(VehicleTally))


@dataclass
class Score:
    """Results of one or more runs; ``add`` pools runs across seeds."""

    runs: int = 0
    failed_runs: int = 0
    vehicles: list[VehicleTally] = field(default_factory=list)
    delays_wall_us: dict[str, list[int]] = field(default_factory=dict)       # by anomaly kind
    delays_operating_us: dict[str, list[int]] = field(default_factory=dict)  # by anomaly kind
    drift_bias_c: list[float] = field(default_factory=list)
    by_kind: dict[str, list[int]] = field(default_factory=dict)       # kind -> [events, detected]
    conditions: dict[str, list[int]] = field(default_factory=dict)    # nuisance kind ->
    # [rows, assessable rows, false alerts, eligible exposure us]

    def add(self, other: Score) -> None:
        self.runs += other.runs
        self.failed_runs += other.failed_runs
        self.vehicles.extend(other.vehicles)
        for mine, theirs in ((self.delays_wall_us, other.delays_wall_us),
                             (self.delays_operating_us, other.delays_operating_us)):
            for kind, values in theirs.items():
                mine.setdefault(kind, []).extend(values)
        self.drift_bias_c.extend(other.drift_bias_c)
        for target, source, width in ((self.by_kind, other.by_kind, 2), (self.conditions, other.conditions, 4)):
            for kind, values in source.items():
                accumulator = target.setdefault(kind, [0] * width)
                for i, value in enumerate(values):
                    accumulator[i] += value


def _detection_end(event: Event) -> int:
    if event.horizon_us is None:
        return event.end_us
    return min(event.end_us, event.onset_us + event.horizon_us)


def _eligible(t: int, fault_windows: tuple[tuple[int, int], ...]) -> bool:
    """A false alert is possible at ``t`` only outside every fault interval."""
    return not any(onset <= t <= end for onset, end in fault_windows)


def _matches(event: Event, direction: int) -> bool:
    return event.direction == 0 or direction == event.direction


def score(verdicts: Sequence[Verdict] | None, truth: GroundTruth, *, segment_gap_s: float = 900.0) -> Score:
    """Score one detector run on one dataset. ``None`` means the run failed."""
    result = Score(runs=1)
    if verdicts is None:
        result.failed_runs = 1
        return result
    if len(verdicts) != len(truth.observation_epochs_us):
        raise ValueError("verdicts must align 1:1 with observation rows")
    gap_us = round(segment_gap_s * US)
    rows_by_vehicle: dict[str, list[tuple[int, int, str, int]]] = defaultdict(list)
    for index, (vehicle, epoch_us, (status, direction)) in enumerate(
            zip(truth.observation_vehicles, truth.observation_epochs_us, verdicts)):
        rows_by_vehicle[vehicle].append((epoch_us, index, status, direction))
    events_by_vehicle: dict[str, list[Event]] = defaultdict(list)
    for event in truth.events:
        events_by_vehicle[event.vehicle_id].append(event)
    faulty = {e.vehicle_id for e in truth.events if e.category != "nuisance"}
    tallies: dict[str, VehicleTally] = {}

    for vehicle in sorted(rows_by_vehicle):
        rows = rows_by_vehicle[vehicle]
        rows.sort()
        tally = tallies[vehicle] = VehicleTally()
        result.vehicles.append(tally)
        times = [r[0] for r in rows]
        n = len(rows)
        events = events_by_vehicle.get(vehicle, [])
        justified = [e for e in events if e.category != "nuisance"]
        nuisance = [e for e in events if e.category == "nuisance"]
        fault_windows = tuple((e.onset_us, e.end_us) for e in justified)

        operating = [0] * n
        segment_of = [0] * n
        for i in range(1, n):
            delta = times[i] - times[i - 1]
            if delta > gap_us:
                segment_of[i] = segment_of[i - 1] + 1
                operating[i] = operating[i - 1]
                continue
            segment_of[i] = segment_of[i - 1]
            operating[i] = operating[i - 1] + delta
            if _eligible(times[i - 1], fault_windows) and _eligible(times[i], fault_windows):
                tally.eligible_exposure_us += delta
                for e in nuisance:
                    if e.onset_us <= times[i - 1] and times[i] <= e.end_us:
                        result.conditions.setdefault(e.kind, [0, 0, 0, 0])[3] += delta

        tally.rows = n
        tally.assessable = sum(1 for r in rows if r[2] != "insufficient_evidence")
        tally.exposure_us = operating[-1] if n else 0
        if vehicle in truth.vehicles_without_reference:
            tally.unseen_rows = n
            tally.unseen_rows_normal = sum(1 for r in rows if r[2] == "normal")

        # Alerts and their attribution.
        false_alert_segments: set[int] = set()
        current: int | None = None
        for i, (t, _, status, direction) in enumerate(rows):
            if i and segment_of[i] != segment_of[i - 1]:
                current = None
            if status == "confirmed_anomaly":
                if current != direction:
                    tally.alerts += 1
                    windows = [e for e in justified if e.onset_us <= t <= e.end_us]
                    if not windows:
                        tally.false_alerts += 1
                        false_alert_segments.add(segment_of[i])
                        cause = next((e.kind for e in nuisance if e.onset_us <= t <= e.end_us), None)
                        if cause is not None:
                            result.conditions.setdefault(cause, [0, 0, 0, 0])[2] += 1
                    elif any(_matches(e, direction) for e in windows):
                        tally.true_alerts += 1
                    else:
                        tally.misdirected_alerts += 1
                current = direction
            elif status in ("normal", "suspected_anomaly"):
                current = None
        first_of_segment: dict[int, int] = {}
        for i in range(n):
            first_of_segment.setdefault(segment_of[i], i)
        for segment, i in first_of_segment.items():
            if _eligible(times[i], fault_windows):
                tally.eligible_trips += 1
                tally.trips_with_false_alert += segment in false_alert_segments

        for event in nuisance:
            lo, hi = bisect_left(times, event.onset_us), bisect_right(times, event.end_us)
            condition = result.conditions.setdefault(event.kind, [0, 0, 0, 0])
            condition[0] += hi - lo
            condition[1] += sum(1 for r in rows[lo:hi] if r[2] != "insufficient_evidence")

        for event in justified:
            if event.category == "sensor_fault":
                lo, hi = bisect_left(times, event.onset_us), bisect_right(times, event.end_us)
                tally.fault_rows += hi - lo
                tally.fault_rows_normal += sum(1 for r in rows[lo:hi] if r[2] == "normal")
                continue
            lo, hi = bisect_left(times, event.onset_us), bisect_right(times, _detection_end(event))
            pair = result.by_kind.setdefault(event.kind, [0, 0])
            pair[0] += 1
            tally.events += 1
            hit = next((i for i in range(lo, hi)
                        if rows[i][2] == "confirmed_anomaly" and _matches(event, rows[i][3])), None)
            if hit is None:
                if all(rows[i][2] == "insufficient_evidence" for i in range(lo, hi)):
                    tally.missed_without_evidence += 1
                continue
            tally.detected += 1
            pair[1] += 1
            result.delays_wall_us.setdefault(event.kind, []).append(times[hit] - event.onset_us)
            result.delays_operating_us.setdefault(event.kind, []).append(operating[hit] - operating[lo])
            if event.kind == "sensor_drift":
                result.drift_bias_c.append(abs(event.param("rate_c_per_day")) * (times[hit] - event.onset_us) / DAY_US)

    # Placebo control: identical windows and directions on fault-free vehicles.
    controls = [v for v in sorted(rows_by_vehicle) if v not in faulty]
    for event in (e for e in truth.events if e.category == "anomaly"):
        end = _detection_end(event)
        for vehicle in controls:
            rows, tally = rows_by_vehicle[vehicle], tallies[vehicle]
            tally.placebo_events += 1
            lo, hi = bisect_left(rows, (event.onset_us,)), bisect_left(rows, (end + 1,))
            if any(r[2] == "confirmed_anomaly" and _matches(event, r[3]) for r in rows[lo:hi]):
                tally.placebo_detected += 1
    return result


# ------------------------------------------------------------- statistics

def _poisson_cdf(k: int, mean: float) -> float:
    if mean <= 0:
        return 1.0
    log_terms = [-mean + i * log(mean) - lgamma(i + 1) for i in range(k + 1)]
    peak = max(log_terms)
    return min(1.0, exp(peak) * sum(exp(term - peak) for term in log_terms))


def poisson_interval(count: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Garwood) confidence interval for a Poisson count, by bisection."""
    def solve(target: float, k: int) -> float:
        low, high = 0.0, max(10.0, 3.0 * k + 30.0)
        for _ in range(200):
            mid = (low + high) / 2
            if _poisson_cdf(k, mid) > target:
                low = mid
            else:
                high = mid
        return (low + high) / 2

    upper = solve(alpha / 2, count)
    lower = 0.0 if count == 0 else solve(1 - alpha / 2, count - 1)
    return lower, upper


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def quantile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank quantile (no interpolation): an observed value, reproducible."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, ceil(round(q * len(ordered), 9)))  # round() guards against 0.1*30 = 3.0000000000000004
    return float(ordered[min(rank, len(ordered)) - 1])


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _rate_per_1000h(count: float, exposure_us: float) -> float | None:
    return 1000 * count / (exposure_us / HOUR_US) if exposure_us else None


def _excess(s: dict[str, int]) -> float | None:
    recall, placebo = _ratio(s["detected"], s["events"]), _ratio(s["placebo_detected"], s["placebo_events"])
    return None if recall is None or placebo is None else recall - placebo


_STATISTICS: dict[str, Callable[[dict[str, int]], float | None]] = {
    "recall": lambda s: _ratio(s["detected"], s["events"]),
    "placebo_recall": lambda s: _ratio(s["placebo_detected"], s["placebo_events"]),
    "excess_recall": _excess,
    "precision": lambda s: _ratio(s["true_alerts"], s["alerts"]),
    "false_alerts_per_1000h": lambda s: _rate_per_1000h(s["false_alerts"], s["eligible_exposure_us"]),
    "trips_with_false_alert": lambda s: _ratio(s["trips_with_false_alert"], s["eligible_trips"]),
    "false_reassurance": lambda s: _ratio(s["fault_rows_normal"], s["fault_rows"]),
    "coverage": lambda s: _ratio(s["assessable"], s["rows"]),
}


def bootstrap_intervals(tallies: Sequence[VehicleTally]) -> dict[str, list[float] | None]:
    """95 % percentile intervals, resampling vehicles with replacement."""
    if not tallies:
        return {name: None for name in _STATISTICS}
    columns = {name: [getattr(t, name) for t in tallies] for name in TALLY_FIELDS}
    rng = random.Random(BOOTSTRAP_SEED)
    count = len(tallies)
    values: dict[str, list[float]] = {name: [] for name in _STATISTICS}
    for _ in range(BOOTSTRAP_RESAMPLES):
        picks = [rng.randrange(count) for _ in range(count)]
        sums = {name: sum(column[i] for i in picks) for name, column in columns.items()}
        for name, statistic in _STATISTICS.items():
            value = statistic(sums)
            if value is not None:
                values[name].append(value)
    out: dict[str, list[float] | None] = {}
    for name, samples in values.items():
        if len(samples) < BOOTSTRAP_RESAMPLES // 2:
            out[name] = None
            continue
        low, high = quantile(samples, 0.025), quantile(samples, 0.975)
        out[name] = None if low is None or high is None else [round(low, 4), round(high, 4)]
    return out


def metrics(total: Score) -> dict[str, object]:
    """Derived metrics; None means not applicable (never a made-up number)."""
    sums = {name: sum(getattr(t, name) for t in total.vehicles) for name in TALLY_FIELDS}
    point = {name: statistic(sums) for name, statistic in _STATISTICS.items()}
    intervals = bootstrap_intervals(total.vehicles)

    def r(value: float | None, digits: int = 4) -> float | None:
        return None if value is None else round(value, digits)

    zero_bound = None
    if sums["false_alerts"] == 0 and sums["eligible_exposure_us"]:
        zero_bound = round(1000 * poisson_interval(0)[1] / (sums["eligible_exposure_us"] / HOUR_US), 3)
    delays = {}
    for kind in sorted(total.delays_operating_us):
        operating, wall = total.delays_operating_us[kind], total.delays_wall_us[kind]
        delays[kind] = {"n": len(operating),
                        "median_operating_min": _minutes(median(operating)),
                        "p90_operating_min": _minutes(quantile(operating, 0.9)),
                        "max_operating_min": _minutes(max(operating)),
                        "median_wall_h": r((median(wall) or 0) / HOUR_US, 3)}
    return {
        "runs": total.runs,
        "failed_runs": total.failed_runs,
        "vehicles": len(total.vehicles),
        "rows": sums["rows"],
        "coverage": r(point["coverage"]),
        "exposure_h": round(sums["exposure_us"] / HOUR_US, 2),
        "eligible_exposure_h": round(sums["eligible_exposure_us"] / HOUR_US, 2),
        "anomaly_events": sums["events"],
        "detected": sums["detected"],
        "recall": r(point["recall"]),
        "recall_ci95": intervals["recall"],
        "placebo_recall": r(point["placebo_recall"]),
        "placebo_recall_ci95": intervals["placebo_recall"],
        "excess_recall": r(point["excess_recall"]),
        "excess_recall_ci95": intervals["excess_recall"],
        "missed_without_evidence": sums["missed_without_evidence"] if sums["events"] else None,
        "alerts": sums["alerts"],
        "true_alerts": sums["true_alerts"],
        "misdirected_alerts": sums["misdirected_alerts"],
        "false_alerts": sums["false_alerts"],
        "precision": r(point["precision"]),
        "precision_ci95": intervals["precision"],
        "false_alerts_per_1000h": r(point["false_alerts_per_1000h"], 3),
        "false_alerts_per_1000h_ci95": intervals["false_alerts_per_1000h"],
        "false_alerts_zero_count_upper_per_1000h": zero_bound,
        "trips_with_false_alert": r(point["trips_with_false_alert"]),
        "trips_with_false_alert_ci95": intervals["trips_with_false_alert"],
        "false_reassurance": r(point["false_reassurance"]),
        "false_reassurance_ci95": intervals["false_reassurance"],
        "normal_rows_on_unseen_vehicles": sums["unseen_rows_normal"] if sums["unseen_rows"] else None,
        "delays_by_kind": delays,
        "median_drift_bias_at_detection_c": r(median(total.drift_bias_c), 3),
        "recall_by_kind": {kind: f"{d}/{e}" for kind, (e, d) in sorted(total.by_kind.items())},
        "conditions": {kind: {"rows": c[0],
                              "coverage": r(_ratio(c[1], c[0])),
                              "false_alerts": c[2],
                              "false_alerts_per_1000h": r(_rate_per_1000h(c[2], c[3]), 3)}
                       for kind, c in sorted(total.conditions.items())},
    }


def _minutes(value_us: float | None) -> float | None:
    return None if value_us is None else round(value_us / US / 60, 2)
