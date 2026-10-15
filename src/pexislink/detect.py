"""Anomaly detection with four explicit evidence states and two barriers.

Statuses
    normal                 assessable against this vehicle's own reference in this
                           operating context, within threshold, below the absolute
                           limit. Means "consistent with the reference", not "healthy".
    suspected_anomaly      a deviation that has not yet been sustained for confirm_s.
    confirmed_anomaly      a same-direction deviation sustained for >= confirm_s with
                           no evidence gap > run_gap_s. Not a diagnosed fault and not
                           a prediction of failure.
    insufficient_evidence  the data cannot support a claim; ``reasons[0]`` says why.

Barriers
    relative  |coolant - reference median| >= threshold (k_scale * robust scale, or
              fixed_threshold_c). Needs a reference for this vehicle and context and
              is suppressed during warm-up.
    absolute  coolant >= absolute_high_c. Reference-free; works for unseen vehicles,
              during warm-up and with a contaminated reference. It can only raise an
              anomaly, never assert normal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Literal

from .ingest import DEFAULT_MAX_VEHICLES, Record
from .quality import Annotated, QualityTracker
from .reference import ReferenceModel, context_key

SCHEMA = "pexislink.assessment.v2"

Status = Literal["normal", "suspected_anomaly", "confirmed_anomaly", "insufficient_evidence"]
STATUSES: tuple[Status, ...] = ("normal", "suspected_anomaly", "confirmed_anomaly",
                                "insufficient_evidence")

_COOLANT_ISSUES = ("coolant_temp_c_missing", "coolant_temp_c_invalid",
                   "coolant_temp_c_sentinel", "coolant_temp_c_implausible")
_CONTEXT_ISSUES = tuple(f"{field}_{kind}"
                        for field in ("engine_rpm", "speed_kmh", "engine_load_pct")
                        for kind in ("missing", "invalid", "implausible"))
_US = 1_000_000


@dataclass(frozen=True, slots=True)
class RecordAssessment:
    line: int
    vehicle_id: str | None
    timestamp: datetime | None
    epoch_us: int | None
    status: Status
    reasons: tuple[str, ...]
    quality: tuple[str, ...]
    coolant_temp_c: float | None = None
    reference_c: float | None = None
    delta_c: float | None = None
    threshold_c: float | None = None
    reference_n: int | None = None
    run_s: float | None = None

    @property
    def direction(self) -> int:
        """+1 reading above expectation, -1 below, 0 for non-anomalous statuses."""
        if self.status not in ("suspected_anomaly", "confirmed_anomaly"):
            return 0
        return 1 if {"above_reference", "above_absolute_limit"} & set(self.reasons) else -1

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "line": self.line,
            "vehicle_id": self.vehicle_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "status": self.status,
            "reasons": list(self.reasons),
            "quality": list(self.quality),
            "coolant_temp_c": self.coolant_temp_c,
            "reference_c": self.reference_c,
            "delta_c": self.delta_c,
            "threshold_c": self.threshold_c,
            "reference_n": self.reference_n,
            "run_s": self.run_s,
        }


class _Run:
    __slots__ = ("direction", "start_us", "last_us")

    def __init__(self, direction: int, now: int) -> None:
        self.direction, self.start_us, self.last_us = direction, now, now


def _first(issues: tuple[str, ...], candidates: tuple[str, ...], default: str) -> str:
    return next((issue for issue in issues if issue in candidates), default)


class Detector:
    """Stateful, single-pass assessor. One instance per observation stream."""

    def __init__(self, model: ReferenceModel, *, max_vehicles: int = DEFAULT_MAX_VEHICLES) -> None:
        self.model = model
        self.config = model.config
        self._tracker = QualityTracker(model.config, max_vehicles=max_vehicles)
        self._runs: dict[str, _Run] = {}
        self._run_gap_us = round(model.config.run_gap_s * _US)
        self._confirm_us = round(model.config.confirm_s * _US)

    def assess(self, record: Record) -> RecordAssessment:
        vehicle = record.vehicle_id
        if vehicle is not None and record.epoch_us is not None:
            end = self.model.reference_end_us.get(vehicle)
            if end is not None and record.epoch_us <= end:
                # Leakage barrier, per record: never assess an observation against a
                # reference that contains later information. The record is excluded
                # from the vehicle's stream rather than aborting the fleet.
                return RecordAssessment(record.line, vehicle, record.timestamp, record.epoch_us,
                                        "insufficient_evidence", ("reference_overlap",), record.issues,
                                        record.coolant_temp_c)
        item = self._tracker.annotate(record)

        if item.integrity != "accepted":
            if item.integrity == "conflicting_duplicate" and vehicle is not None:
                self._runs.pop(vehicle, None)  # evidence chain is no longer trustworthy
            return self._insufficient(item, item.integrity)
        if not item.coolant_plausible:
            return self._insufficient(item, _first(item.issues, _COOLANT_ISSUES, "coolant_unusable"))

        coolant, now = record.coolant_temp_c, record.epoch_us
        if vehicle is None or now is None or coolant is None:
            # Unreachable while quality.py upholds its contract (accepted implies
            # placeable; coolant_plausible implies parsed). Fail safe: never assert normal.
            return self._insufficient(item, "internal_inconsistency")
        cfg = self.config

        # The relative barrier needs a trustworthy reading, a warm engine and a
        # reference; the absolute barrier only needs a plausible reading, so a
        # sensor stuck high still fails towards an alarm.
        blocker: str | None = None
        stats = None
        if not item.coolant_ok:
            blocker = "coolant_stuck_suspected"
        elif item.warmup:
            blocker = "warmup_transient"
        elif vehicle not in self.model.vehicles:
            blocker = "no_reference_vehicle"
        else:
            key = context_key(item, cfg, self.model.use_load)
            if key is None:
                blocker = _first(item.issues, _CONTEXT_ISSUES, "context_unusable")
            else:
                stats = self.model.stats.get(key)
                if stats is None:
                    blocker = "no_reference_context"

        reasons: list[str] = []
        direction: int | None = None
        delta = threshold = None
        if stats is not None:
            delta = coolant - stats.median_c
            threshold = (cfg.fixed_threshold_c if cfg.fixed_threshold_c is not None
                         else cfg.k_scale * stats.scale_c)
            if delta >= threshold:
                direction = 1
                reasons.append("above_reference")
            elif delta <= -threshold:
                direction = -1
                reasons.append("below_reference")
            else:
                direction = 0
        if cfg.absolute_high_c is not None and coolant >= cfg.absolute_high_c:
            reasons.append("above_absolute_limit")
            direction = 1  # any barrier may raise an anomaly; none may veto another

        if direction is None:
            return self._insufficient(item, blocker or "insufficient_reference")

        reference_c = stats.median_c if stats else None
        reference_n = stats.n if stats else None
        if direction == 0:
            self._runs.pop(vehicle, None)
            return RecordAssessment(record.line, vehicle, record.timestamp, now, "normal", (),
                                    item.issues, coolant, reference_c, delta, threshold,
                                    reference_n)

        run = self._runs.get(vehicle)
        if run is None or run.direction != direction or now - run.last_us > self._run_gap_us:
            run = self._runs[vehicle] = _Run(direction, now)
        else:
            run.last_us = now
        held_us = now - run.start_us  # integer microseconds, like the config validation
        status: Status = "confirmed_anomaly" if held_us >= self._confirm_us else "suspected_anomaly"
        return RecordAssessment(record.line, vehicle, record.timestamp, now, status,
                                tuple(reasons), item.issues, coolant, reference_c, delta,
                                threshold, reference_n, held_us / _US)

    def _insufficient(self, item: Annotated, reason: str) -> RecordAssessment:
        record = item.record
        return RecordAssessment(
            line=record.line, vehicle_id=record.vehicle_id, timestamp=record.timestamp,
            epoch_us=record.epoch_us, status="insufficient_evidence", reasons=(reason,),
            quality=item.issues, coolant_temp_c=record.coolant_temp_c)


def assess_records(model: ReferenceModel, records: Iterable[Record], *,
                   max_vehicles: int = DEFAULT_MAX_VEHICLES) -> Iterator[RecordAssessment]:
    detector = Detector(model, max_vehicles=max_vehicles)
    for record in records:
        yield detector.assess(record)
