"""A small, deterministic, read-only telemetry anomaly baseline.

This module detects deviations from an earlier reference period. It does not
predict component failure, estimate remaining useful life, or diagnose faults.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import floor, isfinite
from pathlib import Path
from statistics import median
import csv
from typing import Literal, Mapping, Sequence


REQUIRED_COLUMNS = (
    "timestamp",
    "vehicle_id",
    "engine_rpm",
    "speed_kmh",
    "coolant_temp_c",
)

Status = Literal["normal", "suspected_anomaly", "anomaly", "insufficient_baseline"]
Context = tuple[str, int, int]


@dataclass(frozen=True, slots=True)
class Sample:
    timestamp: datetime
    vehicle_id: str
    engine_rpm: float
    speed_kmh: float
    coolant_temp_c: float

    @classmethod
    def from_mapping(cls, row: Mapping[str, str]) -> Sample:
        timestamp_text = (row.get("timestamp") or "").strip()
        try:
            timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO 8601 datetime") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")

        vehicle_id = (row.get("vehicle_id") or "").strip()
        if not vehicle_id:
            raise ValueError("vehicle_id must not be empty")

        values: dict[str, float] = {}
        for key in ("engine_rpm", "speed_kmh", "coolant_temp_c"):
            try:
                number = float((row.get(key) or "").strip())
            except ValueError as exc:
                raise ValueError(f"{key} must be numeric") from exc
            if not isfinite(number):
                raise ValueError(f"{key} must be finite")
            values[key] = number

        if values["engine_rpm"] < 0 or values["speed_kmh"] < 0:
            raise ValueError("engine_rpm and speed_kmh cannot be negative")
        return cls(timestamp, vehicle_id, **values)


def read_samples(path: str | Path) -> list[Sample]:
    """Load strict, timestamped telemetry records from UTF-8 CSV."""
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing CSV columns: {', '.join(sorted(missing))}")
        samples: list[Sample] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"line {line_number}: unexpected extra CSV fields")
            try:
                samples.append(Sample.from_mapping(row))
            except ValueError as exc:
                raise ValueError(f"line {line_number}: {exc}") from exc
    return samples


def _ensure_increasing(samples: Sequence[Sample], name: str) -> None:
    latest: dict[str, datetime] = {}
    for sample in samples:
        last = latest.get(sample.vehicle_id)
        if last is not None and sample.timestamp <= last:
            raise ValueError(f"{name} timestamps must strictly increase per vehicle")
        latest[sample.vehicle_id] = sample.timestamp


def _context(sample: Sample) -> Context:
    # The operating ranges are intentionally simple and replaceable.
    return (sample.vehicle_id, floor(sample.engine_rpm / 500), floor(sample.speed_kmh / 20))


@dataclass(frozen=True, slots=True)
class Assessment:
    sample: Sample
    status: Status
    reference_c: float | None
    delta_c: float | None


def analyze(
    baseline: Sequence[Sample],
    observations: Sequence[Sample],
    *,
    threshold_c: float = 8.0,
    min_baseline: int = 5,
    persistence: int = 2,
) -> list[Assessment]:
    """Compare later observations with an earlier, context-matched baseline.

    An anomaly is only a persistent absolute temperature deviation. It is NOT
    a diagnosed fault or a prediction of future failure.
    """
    if not isfinite(threshold_c) or threshold_c <= 0:
        raise ValueError("threshold_c must be finite and positive")
    if min_baseline < 2 or persistence < 1:
        raise ValueError("min_baseline >= 2 and persistence >= 1 are required")
    _ensure_increasing(baseline, "baseline")
    _ensure_increasing(observations, "observation")

    last_reference: dict[str, datetime] = {}
    for sample in baseline:
        last_reference[sample.vehicle_id] = sample.timestamp
    for sample in observations:
        end = last_reference.get(sample.vehicle_id)
        if end is not None and sample.timestamp <= end:
            raise ValueError("baseline must be strictly earlier than observations per vehicle")

    by_context: dict[Context, list[float]] = defaultdict(list)
    for sample in baseline:
        by_context[_context(sample)].append(sample.coolant_temp_c)
    reference = {
        key: median(temperatures)
        for key, temperatures in by_context.items()
        if len(temperatures) >= min_baseline
    }

    assessments: list[Assessment] = []
    previous: dict[str, tuple[Context, bool] | None] = {}
    streak: dict[str, int] = defaultdict(int)
    for sample in observations:
        key = _context(sample)
        reference_c = reference.get(key)
        if reference_c is None:
            assessments.append(Assessment(sample, "insufficient_baseline", None, None))
            previous[sample.vehicle_id] = None
            streak[sample.vehicle_id] = 0
            continue

        delta = sample.coolant_temp_c - reference_c
        if abs(delta) < threshold_c:
            status: Status = "normal"
            previous[sample.vehicle_id] = None
            streak[sample.vehicle_id] = 0
        else:
            signature = (key, delta > 0)
            streak[sample.vehicle_id] = (
                streak[sample.vehicle_id] + 1
                if previous.get(sample.vehicle_id) == signature
                else 1
            )
            previous[sample.vehicle_id] = signature
            status = "anomaly" if streak[sample.vehicle_id] >= persistence else "suspected_anomaly"
        assessments.append(Assessment(sample, status, reference_c, delta))
    return assessments
