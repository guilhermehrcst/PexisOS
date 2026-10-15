"""Offline, event-level evaluation against synthetic ground truth.

This is the only module that reads ``GroundTruth``. Detection code must never
import it (enforced by tests/test_architecture.py). Detectors are scored only
from their per-row statuses, aligned 1:1 with the observation rows.

Definitions (identical for every detector)
- Alert: within one operating segment (rows no more than ``segment_gap_s``
  apart), a maximal run of ``confirmed_anomaly`` rows in time order; normal or
  suspected rows end it, insufficient-evidence rows neither end nor extend it.
  Each alert onset is what an operator would be notified about.
- True alert: its onset lies inside an anomaly or sensor-fault interval of the
  same vehicle; otherwise false.
- Detected event: at least one confirmed row inside the event interval.
- Delay: from event onset to the first confirmed row, in wall-clock time and in
  operating time (time covered by data).
- Exposure: operating hours covered by observation rows.
- False reassurance: share of rows inside a sensor-fault interval assessed
  ``normal``.
- Placebo recall: every anomaly interval is copied onto each control vehicle
  (one without anomaly or sensor-fault events) and "detected" the same way.
  A detector that alarms often gets high recall by coincidence; recall is only
  evidence of detection to the extent that it exceeds placebo recall. (Added
  after the dev split showed v0.1 "detecting" drift at 0.14 C of bias.)
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil, exp, lgamma, log, sqrt
from typing import Sequence

from .synth import DAY_US, GroundTruth

US = 1_000_000
Z95 = 1.959963984540054


@dataclass
class Score:
    """Additive counts so that results can be pooled across seeds."""

    runs: int = 0
    failed_runs: int = 0
    rows: int = 0
    assessable: int = 0
    exposure_us: int = 0
    events: int = 0
    detected: int = 0
    placebo_events: int = 0
    placebo_detected: int = 0
    missed_without_evidence: int = 0
    delays_wall_us: list[int] = field(default_factory=list)
    delays_operating_us: list[int] = field(default_factory=list)
    drift_bias_c: list[float] = field(default_factory=list)
    alerts: int = 0
    true_alerts: int = 0
    false_alerts: int = 0
    false_alerts_in_nuisance: int = 0
    fault_rows: int = 0
    fault_rows_normal: int = 0
    unseen_rows: int = 0
    unseen_rows_normal: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # anomaly kind -> [events, detected]
    false_alerts_by_kind: dict[str, int] = field(default_factory=dict)  # nuisance kind -> count

    def add(self, other: Score) -> None:
        for kind, (events, detected) in other.by_kind.items():
            pair = self.by_kind.setdefault(kind, [0, 0])
            pair[0] += events
            pair[1] += detected
        for kind, count in other.false_alerts_by_kind.items():
            self.false_alerts_by_kind[kind] = self.false_alerts_by_kind.get(kind, 0) + count
        for name, value in vars(other).items():
            if isinstance(value, list):
                getattr(self, name).extend(value)
            elif not isinstance(value, dict):
                setattr(self, name, getattr(self, name) + value)


def score(statuses: Sequence[str] | None, truth: GroundTruth, *, segment_gap_s: float = 900.0) -> Score:
    """Score one detector run on one dataset. ``None`` means the run failed."""
    result = Score(runs=1)
    if statuses is None:
        result.failed_runs = 1
        return result
    if len(statuses) != len(truth.observation_epochs_us):
        raise ValueError("statuses must align 1:1 with observation rows")
    gap_us = round(segment_gap_s * US)
    rows_by_vehicle: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for index, (vehicle, epoch_us, status) in enumerate(
            zip(truth.observation_vehicles, truth.observation_epochs_us, statuses)):
        rows_by_vehicle[vehicle].append((epoch_us, index, status))
    events_by_vehicle = defaultdict(list)
    for event in truth.events:
        events_by_vehicle[event.vehicle_id].append(event)

    for vehicle, rows in rows_by_vehicle.items():
        rows.sort()
        times = [t for t, _, _ in rows]
        # cumulative operating time at each row (gaps beyond a segment do not count)
        operating = [0] * len(rows)
        for i in range(1, len(rows)):
            delta = times[i] - times[i - 1]
            operating[i] = operating[i - 1] + (delta if delta <= gap_us else 0)
        result.rows += len(rows)
        result.exposure_us += operating[-1]
        result.assessable += sum(1 for _, _, s in rows if s != "insufficient_evidence")
        if vehicle in truth.vehicles_without_reference:
            result.unseen_rows += len(rows)
            result.unseen_rows_normal += sum(1 for _, _, s in rows if s == "normal")

        events = events_by_vehicle.get(vehicle, [])
        justified = [e for e in events if e.category in ("anomaly", "sensor_fault")]
        nuisance = [e for e in events if e.category == "nuisance"]

        in_alert, previous = False, None
        for t, _, status in rows:
            if previous is not None and t - previous > gap_us:
                in_alert = False
            previous = t
            if status == "confirmed_anomaly":
                if not in_alert:
                    result.alerts += 1
                    if any(e.onset_us <= t <= e.end_us for e in justified):
                        result.true_alerts += 1
                    else:
                        result.false_alerts += 1
                        cause = next((e.kind for e in nuisance if e.onset_us <= t <= e.end_us), None)
                        if cause is not None:
                            result.false_alerts_in_nuisance += 1
                            result.false_alerts_by_kind[cause] = result.false_alerts_by_kind.get(cause, 0) + 1
                in_alert = True
            elif status in ("normal", "suspected_anomaly"):
                in_alert = False

        for event in events:
            lo = bisect_left(times, event.onset_us)
            hi = bisect_left(times, event.end_us + 1)
            window = rows[lo:hi]
            if event.category == "sensor_fault":
                result.fault_rows += len(window)
                result.fault_rows_normal += sum(1 for _, _, s in window if s == "normal")
            if event.category != "anomaly":
                continue
            result.events += 1
            pair = result.by_kind.setdefault(event.kind, [0, 0])
            pair[0] += 1
            hit = next((i for i in range(lo, hi) if rows[i][2] == "confirmed_anomaly"), None)
            if hit is None:
                if all(s == "insufficient_evidence" for _, _, s in window):
                    result.missed_without_evidence += 1
                continue
            result.detected += 1
            pair[1] += 1
            result.delays_wall_us.append(times[hit] - event.onset_us)
            start = operating[lo] if lo < len(rows) else operating[-1]
            result.delays_operating_us.append(operating[hit] - start)
            if event.kind == "sensor_drift":
                result.drift_bias_c.append(abs(event.param("rate_c_per_day")) * (times[hit] - event.onset_us) / DAY_US)

    # Placebo control: the same anomaly windows on vehicles that have no fault.
    faulty = {e.vehicle_id for e in truth.events if e.category in ("anomaly", "sensor_fault")}
    controls = [rows_by_vehicle[v] for v in sorted(rows_by_vehicle) if v not in faulty]
    for event in (e for e in truth.events if e.category == "anomaly"):
        for rows in controls:
            result.placebo_events += 1
            lo = bisect_left(rows, (event.onset_us,))
            hi = bisect_left(rows, (event.end_us + 1,))
            if any(status == "confirmed_anomaly" for _, _, status in rows[lo:hi]):
                result.placebo_detected += 1
    return result


# ------------------------------------------------------------- statistics

def wilson(successes: int, trials: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None if no trials."""
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


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


def metrics(total: Score) -> dict[str, object]:
    """Derived metrics; None means not applicable (never a made-up number)."""
    exposure_h = total.exposure_us / US / 3600
    def rate(count: int) -> float | None:
        return round(1000 * count / exposure_h, 3) if exposure_h > 0 else None
    def ci(pair: tuple[float, float] | None, scale: float = 1.0) -> list[float] | None:
        return None if pair is None else [round(pair[0] * scale, 4), round(pair[1] * scale, 4)]
    fa_low, fa_high = poisson_interval(total.false_alerts)
    return {
        "runs": total.runs,
        "failed_runs": total.failed_runs,
        "rows": total.rows,
        "coverage": round(total.assessable / total.rows, 4) if total.rows else None,
        "exposure_h": round(exposure_h, 2),
        "anomaly_events": total.events,
        "detected": total.detected,
        "recall": round(total.detected / total.events, 4) if total.events else None,
        "recall_ci95": ci(wilson(total.detected, total.events)),
        "placebo_recall": round(total.placebo_detected / total.placebo_events, 4) if total.placebo_events else None,
        "placebo_recall_ci95": ci(wilson(total.placebo_detected, total.placebo_events)),
        "excess_recall": (round(total.detected / total.events - total.placebo_detected / total.placebo_events, 4)
                          if total.events and total.placebo_events else None),
        "missed_without_evidence": total.missed_without_evidence if total.events else None,
        "alerts": total.alerts,
        "true_alerts": total.true_alerts,
        "false_alerts": total.false_alerts,
        "false_alerts_in_nuisance": total.false_alerts_in_nuisance,
        "precision": round(total.true_alerts / total.alerts, 4) if total.alerts else None,
        "precision_ci95": ci(wilson(total.true_alerts, total.alerts)),
        "false_alerts_per_1000h": rate(total.false_alerts),
        "false_alerts_per_1000h_ci95": ([round(1000 * fa_low / exposure_h, 3), round(1000 * fa_high / exposure_h, 3)]
                                         if exposure_h > 0 else None),
        "median_delay_operating_min": _minutes(median(total.delays_operating_us)),
        "p90_delay_operating_min": _minutes(quantile(total.delays_operating_us, 0.9)),
        "median_delay_wall_h": (round(m / US / 3600, 3) if (m := median(total.delays_wall_us)) is not None else None),
        "median_drift_bias_at_detection_c": (round(b, 3) if (b := median(total.drift_bias_c)) is not None else None),
        "false_reassurance": round(total.fault_rows_normal / total.fault_rows, 4) if total.fault_rows else None,
        "normal_rows_on_unseen_vehicles": total.unseen_rows_normal if total.unseen_rows else None,
        "recall_by_kind": {kind: f"{d}/{e}" for kind, (e, d) in sorted(total.by_kind.items())},
        "false_alerts_by_condition": dict(sorted(total.false_alerts_by_kind.items())),
    }


def _minutes(value_us: float | None) -> float | None:
    return None if value_us is None else round(value_us / US / 60, 2)
