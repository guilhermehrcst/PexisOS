"""Frozen per-vehicle, per-operating-context reference statistics.

The reference describes how *this vehicle's coolant reading* behaved, in a given
operating context, during an earlier period that is assumed (not proven) to be
normal. It is built once and never updated from observations, so an anomaly
under evaluation cannot leak into its own reference.

Only accepted, warm, plausible, non-stuck samples below the absolute limit are
used: the reference must describe regulated operation, not transients.

Leakage boundary: ``reference_end_us`` is, per vehicle, the instant of the last
sample that contributed to the statistics. Information can only flow from
those samples, so an observation must come strictly after it to be assessed;
earlier observations are quarantined by the detector (``reference_overlap``).
A glitched reference row that was excluded (e.g. a far-future timestamp)
therefore cannot deny a vehicle all of its observations.
"""

from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import dataclass, field
from math import ceil, floor
from statistics import median
from typing import Iterable, Mapping

from .config import DetectorConfig
from .ingest import DEFAULT_MAX_CONTEXTS, DEFAULT_MAX_VEHICLES, InputError, Record
from .quality import Annotated, CoolantState, QualityTracker

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
    reference_end_us: Mapping[str, int]     # last contributing sample per vehicle (leakage boundary)
    rows: int = 0
    used: int = 0
    excluded: Mapping[str, int] = field(default_factory=dict)
    # established frozen-sensor suspicions after the last reference row, per
    # vehicle; the detector starts from them so they survive the file boundary
    coolant_states: Mapping[str, CoolantState] = field(default_factory=dict)

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
        load_bin = _bin(record.engine_load_pct, config.load_bin_pct, config.load_range_pct[1])
    return (record.vehicle_id,
            _bin(record.engine_rpm, config.rpm_bin, config.rpm_range[1]),
            _bin(record.speed_kmh, config.speed_bin_kmh, config.speed_range_kmh[1]),
            load_bin)


def _bin(value: float, width: float, maximum: float) -> int:
    """Bin index; the inclusive range maximum (100 %, 10000 rpm, 300 km/h) joins the top bin."""
    return min(floor(value / width), ceil(maximum / width) - 1)


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
                    max_vehicles: int = DEFAULT_MAX_VEHICLES,
                    max_contexts: int = DEFAULT_MAX_CONTEXTS) -> ReferenceModel:
    tracker = QualityTracker(config, max_vehicles=max_vehicles)
    values: dict[ContextKey, array[float]] = {}
    last_us: dict[ContextKey, int] = {}
    excluded: Counter[str] = Counter()
    rows = used = 0
    for record in records:
        rows += 1
        item = tracker.annotate(record)
        key = context_key(item, config, use_load)
        reason = _exclusion(item, config, key)
        coolant = record.coolant_temp_c
        if reason is not None or key is None or coolant is None or record.epoch_us is None:
            excluded[reason or "context_unusable"] += 1
            continue
        series = values.get(key)
        if series is None:
            if len(values) >= max_contexts:
                raise InputError(f"line {record.line}: more than {max_contexts} reference contexts")
            series = values[key] = array("d")
        series.append(coolant)
        used += 1
        # accepted records are strictly increasing per vehicle, so this is the maximum
        last_us[key] = record.epoch_us

    stats = {key: robust_stats(series, config.scale_floor_c)
             for key, series in values.items() if len(series) >= config.min_reference_samples}
    end_us: dict[str, int] = {}  # leakage boundary: only contexts that contribute to the model
    for key in stats:
        end_us[key[0]] = max(end_us.get(key[0], last_us[key]), last_us[key])
    return ReferenceModel(
        config=config,
        use_load=use_load,
        stats=stats,
        vehicles=frozenset(key[0] for key in stats),
        reference_end_us=end_us,
        rows=rows,
        used=used,
        excluded=dict(excluded),
        coolant_states=tracker.coolant_states(),
    )
