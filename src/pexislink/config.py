"""The single, validated source of truth for every v0.2 detector parameter.

Defaults were fixed before the held-out benchmark was run (pre-registration);
see docs/adr/0001-v0.2-offline-detector-architecture.md for the rationale.
They are research defaults, not validated automotive limits.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from math import isfinite
from pathlib import Path
from typing import Any

from .ingest import read_bounded_text

Range = tuple[float, float]


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    # Time and data-quality semantics
    segment_gap_s: float = 900.0
    warmup_s: float = 900.0
    stuck_gap_s: float = 3600.0
    stuck_min_samples: int = 5
    flatline_s: float = 2700.0
    coolant_range_c: Range = (-40.0, 150.0)
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
        positive = ("segment_gap_s", "stuck_gap_s", "flatline_s", "rpm_bin", "speed_bin_kmh",
                    "load_bin_pct", "scale_floor_c", "k_scale", "run_gap_s", "confirm_s")
        for name in positive:
            _require(_is_number(getattr(self, name)) and getattr(self, name) > 0,
                     f"{name} must be a finite positive number")
        _require(_is_number(self.warmup_s) and self.warmup_s >= 0,
                 "warmup_s must be a finite number >= 0")
        for name in ("stuck_min_samples", "min_reference_samples"):
            value = getattr(self, name)
            _require(isinstance(value, int) and not isinstance(value, bool) and value >= 2,
                     f"{name} must be an integer >= 2")
        for name in ("coolant_range_c", "rpm_range", "speed_range_kmh", "load_range_pct"):
            low_high = getattr(self, name)
            _require(isinstance(low_high, tuple) and len(low_high) == 2
                     and all(_is_number(v) for v in low_high) and low_high[0] < low_high[1],
                     f"{name} must be a (low, high) pair of finite numbers with low < high")
        for name in ("fixed_threshold_c", "absolute_high_c"):
            value = getattr(self, name)
            _require(value is None or (_is_number(value) and value > 0),
                     f"{name} must be null or a finite positive number")
        _require(isinstance(self.use_load, bool), "use_load must be a boolean")
        # Guarantees >= 3 deviating samples per confirmation (two samples can span
        # at most run_gap_s < confirm_s) and that a segment break always breaks a run.
        _require(self.run_gap_s < self.confirm_s, "run_gap_s must be smaller than confirm_s")
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
        return cls.from_dict(json.loads(read_bounded_text(path, max_bytes=1 << 20)))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
