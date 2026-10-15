"""Frozen per-vehicle, per-operating-context reference statistics.

The reference describes how *this vehicle's coolant reading* behaved, in a given
operating context, during an earlier period that is assumed (not proven) to be
normal. It is built once and never updated from observations, so an anomaly
under evaluation cannot leak into its own reference.

Only accepted, warm, plausible, non-stuck samples below the absolute limit are
used: the reference must describe regulated operation, not transients.
"""

from __future__ import annotations

from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import ceil, floor
from statistics import median
from typing import Iterable, Mapping

from .config import DetectorConfig
from .ingest import DEFAULT_MAX_VEHICLES, InputError, Record
from .quality import Annotated, QualityTracker

# (vehicle_id, rpm bin, speed bin, load bin or None when load is not used)
ContextKey = tuple[str, int, int, int | None]

MAD_TO_SIGMA = 1.4826  # consistency factor for Gaussian data


@dataclass(frozen=True, slots=True)
class ContextStats:
    n: int
    median_c: float
    scale_c: float  # max(1.4826 * MAD, scale_floor_c)


@dataclass(frozen=True)
class ReferenceModel:
    config: DetectorConfig
    use_load: bool
    stats: Mapping[ContextKey, ContextStats]
    vehicles: frozenset[str]                # vehicles with at least one usable context
    last_reference_us: Mapping[str, int]    # latest reference instant per vehicle (leakage guard)
    rows: int = 0
    used: int = 0
    excluded: Mapping[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "samples_used": self.used,
            "samples_excluded_by_reason": dict(sorted(self.excluded.items())),
            "vehicles_with_reference": len(self.vehicles),
            "contexts": len(self.stats),
            "uses_engine_load": self.use_load,
        }


def context_key(item: Annotated, config: DetectorConfig, use_load: bool) -> ContextKey | None:
    """Operating-context bucket, shared by reference building and detection."""
    record = item.record
    if not item.context_ok or record.vehicle_id is None:
        return None
    if record.engine_rpm is None or record.speed_kmh is None:
        return None
    load_bin: int | None = None
    if use_load:
        if not item.load_ok or record.engine_load_pct is None:
            return None
        top_bin = ceil(config.load_range_pct[1] / config.load_bin_pct) - 1  # 100 % joins the top bin
        load_bin = min(floor(record.engine_load_pct / config.load_bin_pct), top_bin)
    return (record.vehicle_id,
            floor(record.engine_rpm / config.rpm_bin),
            floor(record.speed_kmh / config.speed_bin_kmh),
            load_bin)


def robust_stats(values: Iterable[float], scale_floor_c: float) -> ContextStats:
    ordered = sorted(values)
    centre = median(ordered)
    mad = median(abs(value - centre) for value in ordered)
    return ContextStats(len(ordered), centre, max(MAD_TO_SIGMA * mad, scale_floor_c))


def _exclusion(item: Annotated, config: DetectorConfig, key: ContextKey | None) -> str | None:
    if item.integrity != "accepted":
        return item.integrity
    if not item.coolant_ok:
        return "coolant_unusable"
    if item.warmup:
        return "warmup_transient"
    if key is None:
        return "context_unusable"
    coolant = item.record.coolant_temp_c
    if config.absolute_high_c is not None and coolant is not None and coolant >= config.absolute_high_c:
        return "above_absolute_limit"
    return None


def build_reference(records: Iterable[Record], config: DetectorConfig, *, use_load: bool,
                    max_vehicles: int = DEFAULT_MAX_VEHICLES) -> ReferenceModel:
    tracker = QualityTracker(config, max_vehicles=max_vehicles)
    values: defaultdict[ContextKey, array[float]] = defaultdict(lambda: array("d"))
    last_us: dict[str, int] = {}
    excluded: Counter[str] = Counter()
    rows = used = 0
    for record in records:
        rows += 1
        item = tracker.annotate(record)
        if record.vehicle_id is not None and record.epoch_us is not None:
            previous = last_us.get(record.vehicle_id)
            if previous is None or record.epoch_us > previous:
                last_us[record.vehicle_id] = record.epoch_us
        key = context_key(item, config, use_load)
        reason = _exclusion(item, config, key)
        coolant = record.coolant_temp_c
        if reason is None and key is not None and coolant is not None:
            values[key].append(coolant)
            used += 1
        else:
            excluded[reason or "context_unusable"] += 1

    stats = {key: robust_stats(series, config.scale_floor_c)
             for key, series in values.items() if len(series) >= config.min_reference_samples}
    return ReferenceModel(
        config=config,
        use_load=use_load,
        stats=stats,
        vehicles=frozenset(key[0] for key in stats),
        last_reference_us=last_us,
        rows=rows,
        used=used,
        excluded=dict(excluded),
    )


def check_chronology(model: ReferenceModel, records: Iterable[Record]) -> None:
    """Leakage guard: every observation must be strictly after its vehicle's
    reference period. Raises ``InputError`` on the first violation."""
    for record in records:
        if record.vehicle_id is None or record.epoch_us is None:
            continue
        end = model.last_reference_us.get(record.vehicle_id)
        if end is not None and record.epoch_us <= end:
            raise InputError(
                f"line {record.line}: observation is not after the end of vehicle "
                f"{record.vehicle_id!r}'s reference period (chronological leakage guard)")
