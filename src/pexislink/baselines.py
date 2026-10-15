"""Comparators for the benchmark. Label-blind: they receive telemetry rows only.

- ``legacy_v01``: the frozen v0.1 rule. ``cleaned=False`` reproduces v0.1 as
  shipped (the whole run fails if v0.1 rejects any row). ``cleaned=True`` gives
  v0.1 the benefit of the doubt with oracle-free preprocessing: rows it cannot
  parse are skipped, each vehicle's rows are time-sorted and only the first row
  of each instant is kept.
- ``fixed_limit``: the simplest possible rule, a warning-light threshold with
  a two-sample debounce and no reference, time or quality semantics. It uses the
  same 115 C as v0.2's absolute barrier, so comparing the two isolates what the
  relative barrier and the quality layer add.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .core import Sample, analyze
from .ingest import REQUIRED_COLUMNS

V01_STATUS = {"normal": "normal", "suspected_anomaly": "suspected_anomaly",
              "anomaly": "confirmed_anomaly", "insufficient_baseline": "insufficient_evidence"}


def _parse_v01(rows: Sequence[Sequence[str]]) -> list[Sample | None]:
    parsed: list[Sample | None] = []
    for values in rows:
        try:
            parsed.append(Sample.from_mapping(dict(zip(REQUIRED_COLUMNS, values))))
        except ValueError:
            parsed.append(None)
    return parsed


def _clean(parsed: list[Sample | None]) -> list[tuple[int, Sample]]:
    usable = [(s.vehicle_id, s.timestamp, i, s) for i, s in enumerate(parsed) if s is not None]
    usable.sort(key=lambda item: (item[0], item[1], item[2]))
    out: list[tuple[int, Sample]] = []
    last = None
    for vehicle, moment, index, sample in usable:
        if (vehicle, moment) != last:
            out.append((index, sample))
            last = (vehicle, moment)
    return out


def legacy_v01(reference_rows: Sequence[Sequence[str]], observation_rows: Sequence[Sequence[str]],
               *, cleaned: bool) -> list[str] | None:
    reference, observed = _parse_v01(reference_rows), _parse_v01(observation_rows)
    if not cleaned:
        if any(s is None for s in reference) or any(s is None for s in observed):
            return None
        try:
            result = analyze([s for s in reference if s], [s for s in observed if s])
        except ValueError:
            return None
        return [V01_STATUS[a.status] for a in result]
    kept = _clean(observed)
    result = analyze([s for _, s in _clean(reference)], [s for _, s in kept])
    statuses = ["insufficient_evidence"] * len(observation_rows)
    for (index, _), assessment in zip(kept, result):
        statuses[index] = V01_STATUS[assessment.status]
    return statuses


def fixed_limit(observation_rows: Sequence[Sequence[str]], *, limit_c: float = 115.0,
                persistence: int = 2) -> list[str]:
    statuses = ["insufficient_evidence"] * len(observation_rows)
    streak: defaultdict[str, int] = defaultdict(int)
    for index, sample in _clean(_parse_v01(observation_rows)):
        if sample.coolant_temp_c >= limit_c:
            streak[sample.vehicle_id] += 1
            statuses[index] = "confirmed_anomaly" if streak[sample.vehicle_id] >= persistence else "suspected_anomaly"
        else:
            streak[sample.vehicle_id] = 0
            statuses[index] = "normal"
    return statuses
