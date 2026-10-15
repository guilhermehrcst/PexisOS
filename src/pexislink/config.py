"""The single, validated source of truth for every v0.2 detector parameter.

Defaults were written down before any benchmark data existed (ADR 0001 §4);
ADR 0001 §7 records the changes made after independent review. They are
research defaults, not validated automotive limits.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, fields
from math import isfinite
from pathlib import Path
from typing import Any

from .ingest import read_bounded_text

Range = tuple[float, float]

_MAX_SECONDS = 1e9       # ~31 years: keeps integer-microsecond arithmetic in range
_MAX_MAGNITUDE = 1e6     # physical quantities, bins, thresholds
_US = 1_000_000


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    # Time and data-quality semantics
    segment_gap_s: float = 900.0
    warmup_s: float = 900.0
    stuck_gap_s: float = 3600.0
    stuck_min_samples: int = 5
    stuck_tolerance_c: float = 1.0
    coolant_range_c: Range = (-40.0, 215.0)
    coolant_sentinels_c: tuple[float, ...] = (-40.0, 215.0)
    rpm_range: Range = (0.0, 10_000.0)
    speed_range_kmh: Range = (0.0, 300.0)
    load_range_pct: Range = (0.0, 100.0)
    # Reference model
    rpm_bin: float = 500.0
    speed_bin_kmh: float = 20.0
    load_bin_pct: float = 25.0
    use_load: bool = True
    min_reference_samples: int = 30
    scale_floor_c: float = 1.0
    # Detection
    k_scale: float = 4.0
    fixed_threshold_c: float | None = None
    absolute_high_c: float | None = 115.0
    run_gap_s: float = 90.0
    confirm_s: float = 300.0

    def __post_init__(self) -> None:
        for name in ("segment_gap_s", "stuck_gap_s", "run_gap_s", "confirm_s"):
            _require(_within(getattr(self, name), 0, _MAX_SECONDS, low_open=True),
                     f"{name} must be a number in (0, {_MAX_SECONDS:g}] seconds")
        _require(_within(self.warmup_s, 0, _MAX_SECONDS), f"warmup_s must be in [0, {_MAX_SECONDS:g}] seconds")
        for name in ("rpm_bin", "speed_bin_kmh", "load_bin_pct"):
            _require(_within(getattr(self, name), 1e-3, _MAX_MAGNITUDE), f"{name} must be in [0.001, 1e6]")
        for name in ("scale_floor_c", "k_scale"):
            _require(_within(getattr(self, name), 0, 1e3, low_open=True), f"{name} must be in (0, 1000]")
        _require(_within(self.stuck_tolerance_c, 0, 1e3), "stuck_tolerance_c must be in [0, 1000]")
        for name in ("stuck_min_samples", "min_reference_samples"):
            value = getattr(self, name)
            _require(isinstance(value, int) and not isinstance(value, bool) and 2 <= value <= 1_000_000,
                     f"{name} must be an integer in [2, 1000000]")
        for name in ("coolant_range_c", "rpm_range", "speed_range_kmh", "load_range_pct"):
            pair = getattr(self, name)
            _require(isinstance(pair, tuple) and len(pair) == 2
                     and all(_within(v, -_MAX_MAGNITUDE, _MAX_MAGNITUDE) for v in pair) and pair[0] < pair[1],
                     f"{name} must be a (low, high) pair of numbers within +-1e6 with low < high")
        _require(isinstance(self.coolant_sentinels_c, tuple) and len(self.coolant_sentinels_c) <= 16
                 and all(_within(v, -_MAX_MAGNITUDE, _MAX_MAGNITUDE) for v in self.coolant_sentinels_c),
                 "coolant_sentinels_c must be a list of at most 16 numbers")
        _require(self.fixed_threshold_c is None or _within(self.fixed_threshold_c, 0, 1e3, low_open=True),
                 "fixed_threshold_c must be null or in (0, 1000]")
        _require(self.absolute_high_c is None
                 or (_is_number(self.absolute_high_c) and self.absolute_high_c > 0
                     and self.coolant_range_c[0] < self.absolute_high_c < self.coolant_range_c[1]),
                 "absolute_high_c must be null, positive, and strictly inside coolant_range_c "
                 "(otherwise it can never fire)")
        _require(isinstance(self.use_load, bool), "use_load must be a boolean")
        # Compared in integer microseconds, exactly as the detector does: two samples
        # can span at most run_gap < confirm, so a confirmation needs >= 3 samples;
        # and a segment break always breaks an evidence run.
        _require(round(self.run_gap_s * _US) < round(self.confirm_s * _US),
                 "run_gap_s must be smaller than confirm_s")
        _require(self.run_gap_s <= self.segment_gap_s, "run_gap_s must not exceed segment_gap_s")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DetectorConfig:
        """Closed schema: unknown keys are rejected rather than ignored."""
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown configuration key(s): {', '.join(unknown)}")
        values: dict[str, Any] = {key: tuple(value) if isinstance(value, list) else value
                                  for key, value in data.items()}
        return cls(**values)  # __post_init__ validates every value

    @classmethod
    def from_json_file(cls, path: str | Path) -> DetectorConfig:
        text = read_bounded_text(path, max_bytes=1 << 20)
        try:
            data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except RecursionError:
            raise ValueError("configuration JSON is nested too deeply") from None
        return cls.from_dict(data)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    duplicates = sorted(key for key, count in Counter(key for key, _ in pairs).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate configuration key(s): {', '.join(duplicates)}")
    return dict(pairs)


def _is_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except OverflowError:  # integers too large for a float
        return False


def _within(value: object, low: float, high: float, *, low_open: bool = False) -> bool:
    if not _is_number(value) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return (low < number if low_open else low <= number) and number <= high


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
