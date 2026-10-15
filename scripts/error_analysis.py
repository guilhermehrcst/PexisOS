"""Post-hoc error analysis cited in docs/BENCHMARK.md §2.4.

It explains held-out results; it changes no detector, default or criterion.
The one diagnostic that uses non-default settings runs on development seeds.

Usage: PYTHONPATH=src python scripts/error_analysis.py [test|confirm]
(test seeds 201-210 by default; ``confirm`` uses the confirmation seeds 301-310)
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from math import comb
from typing import Sequence

from pexislink.baselines import Verdict, fixed_limit, legacy_v01
from pexislink.benchmark import BASE, DEV_SEEDS, TEST_SEEDS
from pexislink.config import DetectorConfig
from pexislink.detect import RecordAssessment, assess_records
from pexislink.evaluate import Score, metrics, score
from pexislink.ingest import Record, parse_rows
from pexislink.reference import build_reference
from pexislink.synth import Dataset, Event, generate
from run_confirmation import CONFIRMATION_SEEDS

US = 1_000_000
SEGMENT_GAP_US = round(BASE.segment_gap_s * US)
Row = tuple[int, int, str, int]  # (epoch_us, row index, status, direction)


def assess(data: Dataset, config: DetectorConfig = BASE) -> list[RecordAssessment]:
    model = build_reference(list(parse_rows(data.reference_rows)), config, use_load=config.use_load)
    return list(assess_records(model, list(parse_rows(data.observation_rows))))


def by_vehicle(data: Dataset, verdicts: Sequence[Verdict]) -> dict[str, list[Row]]:
    rows: dict[str, list[Row]] = defaultdict(list)
    truth = data.truth
    for index, (vehicle, epoch_us, (status, direction)) in enumerate(
            zip(truth.observation_vehicles, truth.observation_epochs_us, verdicts)):
        rows[vehicle].append((epoch_us, index, status, direction))
    for vehicle_rows in rows.values():
        vehicle_rows.sort()
    return rows


def alert_onsets(rows: list[Row]) -> list[tuple[int, int, int]]:
    """(onset, direction, trip start) of each alert, as defined in evaluate.score."""
    onsets, current, trip_start = [], None, 0
    for k, (t, _, status, direction) in enumerate(rows):
        if k == 0 or t - rows[k - 1][0] > SEGMENT_GAP_US:
            current, trip_start = None, t
        if status == "confirmed_anomaly":
            if current != direction:
                onsets.append((t, direction, trip_start))
            current = direction
        elif status in ("normal", "suspected_anomaly"):
            current = None
    return onsets


def window(event: Event) -> tuple[int, int]:
    end = event.end_us if event.horizon_us is None else min(event.end_us, event.onset_us + event.horizon_us)
    return event.onset_us, end


def spread(values: list[float]) -> str:
    if not values:
        return "none"
    return f"n={len(values)} min={min(values):.1f} median={statistics.median(values):.1f} max={max(values):.1f}"


def false_alert_mechanism(seeds: Sequence[int]) -> None:
    print("1. v0.2 false-alert onsets: direction and minutes into the trip")
    for scenario in ("seasonal_shift", "stress_slow_warmup_x0.6", "stress_slow_warmup_x0.5"):
        minutes: dict[tuple[str, int], list[float]] = defaultdict(list)
        for seed in seeds:
            data = generate(scenario, seed)
            verdicts = [(a.status, a.direction) for a in assess(data)]
            condition = {e.vehicle_id: e.kind for e in data.truth.events if e.category == "nuisance"}
            faults = [e for e in data.truth.events if e.category != "nuisance"]
            for vehicle, rows in by_vehicle(data, verdicts).items():
                for onset, direction, trip_start in alert_onsets(rows):
                    if any(e.vehicle_id == vehicle and e.onset_us <= onset <= e.end_us for e in faults):
                        continue
                    minutes[(condition.get(vehicle, "none"), direction)].append((onset - trip_start) / 60 / US)
        for (condition_kind, direction), values in sorted(minutes.items()):
            print(f"   {scenario:24s} condition={condition_kind:12s} direction={direction:+d}  {spread(values)}")


def v01_alert_onsets(seeds: Sequence[int]) -> None:
    minutes: list[float] = []
    for seed in seeds:
        data = generate("normal_operation", seed)
        verdicts = legacy_v01(data.reference_rows, data.observation_rows, cleaned=True)
        assert verdicts is not None
        for rows in by_vehicle(data, verdicts).values():
            minutes.extend((onset - start) / 60 / US for onset, _, start in alert_onsets(rows))
    early = sum(1 for m in minutes if m < 15)
    print(f"2. v0.1-cleaned, normal operation: {early}/{len(minutes)} alert onsets "
          f"({early / len(minutes):.1%}) in the first 15 min of a trip; minutes into trip {spread(minutes)}")


def missed_abrupt_events(seeds: Sequence[int]) -> None:
    print("3. abrupt_anomaly events v0.2 missed (window: onset to onset + 24 h)")
    for seed in seeds:
        data = generate("abrupt_anomaly", seed)
        assessed = assess(data)
        rows = by_vehicle(data, [(a.status, a.direction) for a in assessed])
        for event in data.truth.events:
            if event.category != "anomaly":
                continue
            lo, hi = window(event)
            inside = [r for r in rows[event.vehicle_id] if lo <= r[0] <= hi]
            if any(r[2] == "confirmed_anomaly" and r[3] == event.direction for r in inside):
                continue
            later = any(r[2] == "confirmed_anomaly" and r[3] == event.direction
                        for r in rows[event.vehicle_id] if hi < r[0] <= event.end_us)
            runs = [assessed[r[1]].run_s or 0.0 for r in inside]
            reasons = Counter((assessed[r[1]].reasons or ("-",))[0] for r in inside
                              if assessed[r[1]].status == "insufficient_evidence")
            print(f"   seed {seed} {event.vehicle_id} {event.kind:22s} delta {event.param('delta_c'):4.1f} C  "
                  f"statuses {dict(Counter(r[2] for r in inside))}  longest run {max(runs, default=0):.0f} s  "
                  f"abstention reasons {dict(reasons.most_common(3))}  confirmed after the horizon: {later}")


def sampling_rate_diagnostic() -> None:
    print("4. 60 s sampling on DEVELOPMENT seeds with non-default settings (diagnostic; defaults unchanged)")
    variants = {
        "defaults": BASE,
        "min_reference_samples=15": replace(BASE, min_reference_samples=15),
        "min_reference_samples=15, run_gap_s=150": replace(BASE, min_reference_samples=15, run_gap_s=150.0),
    }
    for scenario, names in (("abrupt_anomaly", ("defaults",)), ("stress_sampling_60s", tuple(variants))):
        for name in names:
            total = Score()
            for seed in DEV_SEEDS:
                data = generate(scenario, seed)
                total.add(score([(a.status, a.direction) for a in assess(data, variants[name])], data.truth))
            m = metrics(total)
            print(f"   {scenario:20s} {name:40s} recall {m['recall']}  placebo {m['placebo_recall']}  "
                  f"false alerts {m['false_alerts']}  coverage {m['coverage']}")


def reference_thresholds(seeds: Sequence[int]) -> None:
    print("5. stress_subtle_overheat reference thresholds (k_scale x robust scale)")
    for name, config in (("v0.2", BASE), ("v0.2-no-warmup", replace(BASE, warmup_s=0.0))):
        thresholds: list[float] = []
        for seed in seeds:
            data = generate("stress_subtle_overheat", seed)
            model = build_reference(list(parse_rows(data.reference_rows)), config, use_load=config.use_load)
            thresholds.extend(config.k_scale * stats.scale_c for stats in model.stats.values())
        floor = config.k_scale * config.scale_floor_c
        at_floor = sum(t <= floor + 1e-9 for t in thresholds) / len(thresholds)
        print(f"   {name:15s} contexts {len(thresholds)}  median threshold {statistics.median(thresholds):.2f} C  "
              f"share at the {floor:g} C floor {at_floor:.2f}")


def unseen_vehicle_overheats(seeds: Sequence[int]) -> None:
    print("6. unseen_vehicles_and_regimes overheats")
    for seed in seeds:
        data = generate("unseen_vehicles_and_regimes", seed)
        assessed = assess(data)
        rows = by_vehicle(data, [(a.status, a.direction) for a in assessed])
        limit_verdicts = fixed_limit(data.observation_rows)
        limit_rows = by_vehicle(data, limit_verdicts)
        for event in data.truth.events:
            if event.category != "anomaly":
                continue
            lo, hi = window(event)
            detected = any(r[2] == "confirmed_anomaly" and r[3] == 1 for r in rows[event.vehicle_id] if lo <= r[0] <= hi)
            first_limit = next((r[0] for r in limit_rows[event.vehicle_id]
                                if r[0] >= lo and r[2] == "confirmed_anomaly"), None)
            consecutive_hot = sum(1 for r in limit_rows[event.vehicle_id]
                                  if lo <= r[0] <= hi and r[2] == "confirmed_anomaly")
            reference = "no " if event.vehicle_id in data.truth.vehicles_without_reference else "yes"
            delay = "never" if first_limit is None else f"{(first_limit - lo) / 3600 / US:.1f} h"
            print(f"   seed {seed} {event.vehicle_id} reference {reference} delta {event.param('delta_c'):4.1f} C  "
                  f"v0.2 detected {detected}  rows confirmed by the 115 C limit within 24 h {consecutive_hot}  "
                  f"first 115 C limit alert after {delay}")


FAULT_SCENARIOS = ("abrupt_anomaly", "gradual_sensor_drift", "stress_subtle_overheat", "unseen_vehicles_and_regimes",
                   "data_integrity", "contaminated_reference", "stress_abrupt_noise_x2", "stress_sampling_60s")


def detections(data: Dataset, reference: list[Record], observed: list[Record], config: DetectorConfig) -> list[bool]:
    """Per anomaly event, in ground-truth order: detected as defined in evaluate.score."""
    model = build_reference(reference, config, use_load=config.use_load)
    rows = by_vehicle(data, [(a.status, a.direction) for a in assess_records(model, observed)])
    hits = []
    for event in (e for e in data.truth.events if e.category == "anomaly"):
        lo, hi = window(event)
        hits.append(any(r[2] == "confirmed_anomaly" and (event.direction == 0 or r[3] == event.direction)
                        for r in rows[event.vehicle_id] if lo <= r[0] <= hi))
    return hits


def sign_test(b: int, c: int) -> float:
    """Exact two-sided sign test on discordant pairs."""
    n = b + c
    return 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def paired_comparisons(seeds: Sequence[int]) -> None:
    print("7. Paired detection of the same events (exact two-sided sign test on discordant events)")
    comparisons = [("stress_subtle_overheat", "v0.2-fixed-4C", replace(BASE, fixed_threshold_c=4.0))]
    comparisons += [(scenario, "v0.2-no-load", replace(BASE, use_load=False)) for scenario in FAULT_SCENARIOS]
    for scenario, name, other in comparisons:
        only_v02 = only_other = total_v02 = total_other = events = 0
        for seed in seeds:
            data = generate(scenario, seed)
            reference, observed = list(parse_rows(data.reference_rows)), list(parse_rows(data.observation_rows))
            for a, b in zip(detections(data, reference, observed, BASE), detections(data, reference, observed, other)):
                events += 1
                total_v02 += a
                total_other += b
                only_v02 += a and not b
                only_other += b and not a
        print(f"   {scenario:28s} v0.2 {total_v02}/{events}  {name} {total_other}/{events}  "
              f"only v0.2 {only_v02}  only {name} {only_other}  p={sign_test(only_v02, only_other):.4f}")


def residual_false_reassurance(seeds: Sequence[int]) -> None:
    print("8. Frozen-sensor rows reported normal, before and after the first engine-off gap >= stuck_gap_s")
    gap_us = round(BASE.stuck_gap_s * US)
    for scenario in ("unreliable_sensor", "stress_stuck_flicker"):
        before = after = total = 0
        for seed in seeds:
            data = generate(scenario, seed)
            rows = by_vehicle(data, [(a.status, a.direction) for a in assess(data)])
            for event in (e for e in data.truth.events if e.category == "sensor_fault"):
                parked, previous = False, None
                for t, _, status, _ in (r for r in rows[event.vehicle_id] if r[0] >= event.onset_us):
                    parked = parked or (previous is not None and t - previous >= gap_us)
                    previous, total = t, total + 1
                    if status == "normal":
                        after += parked
                        before += not parked
        print(f"   {scenario:22s} frozen-sensor rows {total}  normal before the first park {before}  after it {after}")


def main(argv: list[str]) -> int:
    seed_sets = {"test": TEST_SEEDS, "confirm": CONFIRMATION_SEEDS}
    if len(argv) > 1 or (argv and argv[0] not in seed_sets):
        print("usage: error_analysis.py [test|confirm]", file=sys.stderr)
        return 2
    seeds = seed_sets[argv[0] if argv else "test"]
    print(f"Seeds {seeds[0]}-{seeds[-1]} (section 4: development seeds {DEV_SEEDS[0]}-{DEV_SEEDS[-1]})")
    false_alert_mechanism(seeds)
    v01_alert_onsets(seeds)
    missed_abrupt_events(seeds)
    sampling_rate_diagnostic()
    reference_thresholds(seeds)
    unseen_vehicle_overheats(seeds)
    paired_comparisons(seeds)
    residual_false_reassurance(seeds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
