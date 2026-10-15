"""Shared builders for v0.2 tests. Rows use the canonical column order:
timestamp, vehicle_id, engine_rpm, speed_kmh, coolant_temp_c, engine_load_pct.
"""

from datetime import datetime, timedelta, timezone

from pexislink.config import DetectorConfig
from pexislink.ingest import parse_rows

BASE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

# Relaxed configuration for focused unit tests: no warm-up suppression, small
# reference requirement. Tests of the defaults use DetectorConfig() directly.
FOCUSED = DetectorConfig(warmup_s=0.0, min_reference_samples=5)


def stamp(seconds: float, *, offset_hours: int = 0) -> str:
    moment = BASE + timedelta(seconds=seconds)
    if offset_hours:
        zone = timezone(timedelta(hours=offset_hours))
        return moment.astimezone(zone).isoformat()
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + (f".{moment.microsecond:06d}" if moment.microsecond else "") + "Z"


def row(seconds, coolant, *, vehicle="v1", rpm=2000, speed=60, load=30, offset_hours=0):
    def text(value):
        return "" if value is None else str(value)
    return (stamp(seconds, offset_hours=offset_hours), vehicle, text(rpm), text(speed),
            text(coolant), text(load))


def records(rows):
    return list(parse_rows(rows))


def steady(start_s, count, coolant=90, *, step_s=30, **kwargs):
    """``count`` rows every ``step_s`` seconds with a constant or callable coolant."""
    out = []
    for i in range(count):
        value = coolant(i) if callable(coolant) else coolant
        out.append(row(start_s + i * step_s, value, **kwargs))
    return out


def alternating(start_s, count, *, low=90, high=91, step_s=30, **kwargs):
    """Realistic quantised noise so stuck/flatline rules are not triggered."""
    return steady(start_s, count, lambda i: low if i % 2 == 0 else high, step_s=step_s, **kwargs)
