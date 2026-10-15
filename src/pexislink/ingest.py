"""Telemetry ingestion: bounded reads, a closed CSV schema, strict row syntax.

This module answers two questions only: *is this file structurally what we
expect?* and *what does each row literally say?* It knows nothing about engine
physics, time gaps or vehicles' history; those belong to ``quality``.

File-level problems (size limits, encoding, header, CSV structure) raise
``InputError`` because the file as a whole cannot be trusted. Row-level problems
(an empty cell, a malformed number, an unparseable timestamp) are recorded as
issue codes on the ``Record`` so that every input row still produces exactly one
assessment downstream and nothing is dropped silently.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Iterable, Iterator, Sequence

REQUIRED_COLUMNS = ("timestamp", "vehicle_id", "engine_rpm", "speed_kmh", "coolant_temp_c")
OPTIONAL_COLUMNS = ("engine_load_pct",)
CANONICAL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
NUMERIC_FIELDS = ("engine_rpm", "speed_kmh", "coolant_temp_c", "engine_load_pct")

DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_LINE_CHARS = 1024
DEFAULT_MAX_ROWS = 2_000_000
DEFAULT_MAX_VEHICLES = 10_000

# RFC 3339 profile: explicit 'T', seconds, optional fraction up to microseconds,
# mandatory offset. re.ASCII matters: in str patterns \d would match any Unicode
# digit (the v0.1 audit, F-12).
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_VEHICLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", re.ASCII)
_BLANKS = " \t"

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime(2200, 1, 1, tzinfo=timezone.utc)
_MAX_OFFSET = timedelta(hours=14)
_MICROSECOND = timedelta(microseconds=1)


class InputError(ValueError):
    """A file-level input problem; the run must not produce assessments."""


@dataclass(frozen=True, slots=True)
class Limits:
    """Explicit resource bounds. Exceeding any of them is an ``InputError``."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS
    max_rows: int = DEFAULT_MAX_ROWS
    max_vehicles: int = DEFAULT_MAX_VEHICLES

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_line_chars", "max_rows", "max_vehicles"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class Record:
    """One input row as literally stated. ``None`` means absent or unusable."""

    line: int
    timestamp: datetime | None
    epoch_us: int | None
    vehicle_id: str | None
    engine_rpm: float | None
    speed_kmh: float | None
    coolant_temp_c: float | None
    engine_load_pct: float | None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ColumnIndex:
    """Position of each canonical field in a row; ``None`` if the column is absent."""

    timestamp: int
    vehicle_id: int
    engine_rpm: int
    speed_kmh: int
    coolant_temp_c: int
    engine_load_pct: int | None
    width: int

    @classmethod
    def from_header(cls, header: Sequence[str]) -> ColumnIndex:
        columns = list(header)
        duplicates = sorted({name for name in columns if columns.count(name) > 1})
        if duplicates:
            raise InputError(f"duplicate CSV column(s): {', '.join(duplicates)}")
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise InputError(f"missing CSV column(s): {', '.join(missing)}")
        unknown = [name for name in columns if name not in CANONICAL_COLUMNS]
        if unknown:
            shown = ", ".join(repr(name[:40]) for name in unknown[:5])
            raise InputError(f"unknown CSV column(s) (closed schema): {shown}")
        position = {name: i for i, name in enumerate(columns)}
        return cls(
            timestamp=position["timestamp"],
            vehicle_id=position["vehicle_id"],
            engine_rpm=position["engine_rpm"],
            speed_kmh=position["speed_kmh"],
            coolant_temp_c=position["coolant_temp_c"],
            engine_load_pct=position.get("engine_load_pct"),
            width=len(columns),
        )


CANONICAL_INDEX = ColumnIndex.from_header(CANONICAL_COLUMNS)
DEFAULT_LIMITS = Limits()


def parse_timestamp(text: str) -> tuple[datetime, int] | None:
    """Return ``(aware datetime, integer microseconds since the Unix epoch)``.

    Integer microseconds make ordering, duplicate detection and differences exact.
    """
    if not _TIMESTAMP.fullmatch(text):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    offset = moment.utcoffset()
    if offset is None or abs(offset) > _MAX_OFFSET:
        return None
    if not EPOCH <= moment < _LATEST:
        return None
    return moment, (moment - EPOCH) // _MICROSECOND


def parse_number(text: str) -> float | None:
    """Strict ASCII decimal; rejects nan/inf, underscores and non-ASCII digits."""
    if not _NUMBER.fullmatch(text):
        return None
    value = float(text)
    return value if isfinite(value) else None


def parse_row(values: Sequence[str], line: int, index: ColumnIndex = CANONICAL_INDEX) -> Record:
    """Parse one row's cells. Never raises for bad cell content."""
    if len(values) != index.width:
        return Record(line, None, None, None, None, None, None, None, ("row_field_count",))

    issues: list[str] = []
    timestamp = epoch_us = None
    text = values[index.timestamp].strip(_BLANKS)
    if not text:
        issues.append("timestamp_missing")
    else:
        parsed = parse_timestamp(text)
        if parsed is None:
            issues.append("timestamp_invalid")
        else:
            timestamp, epoch_us = parsed

    vehicle_id: str | None = values[index.vehicle_id].strip(_BLANKS)
    if not vehicle_id:
        issues.append("vehicle_id_missing")
        vehicle_id = None
    elif not _VEHICLE_ID.fullmatch(vehicle_id):
        issues.append("vehicle_id_invalid")
        vehicle_id = None

    def number(field: str, position: int | None) -> float | None:
        if position is None:  # optional column absent: not a data-quality issue
            return None
        cell = values[position].strip(_BLANKS)
        if not cell:
            issues.append(f"{field}_missing")
            return None
        parsed = parse_number(cell)
        if parsed is None:
            issues.append(f"{field}_invalid")
        return parsed

    rpm = number("engine_rpm", index.engine_rpm)
    speed = number("speed_kmh", index.speed_kmh)
    coolant = number("coolant_temp_c", index.coolant_temp_c)
    load = number("engine_load_pct", index.engine_load_pct)
    return Record(line, timestamp, epoch_us, vehicle_id, rpm, speed, coolant, load, tuple(issues))


def parse_rows(rows: Iterable[Sequence[str]], index: ColumnIndex = CANONICAL_INDEX,
               *, first_line: int = 2) -> Iterator[Record]:
    """Parse already-split rows (e.g. from the synthetic generator)."""
    for line, values in enumerate(rows, start=first_line):
        yield parse_row(values, line, index)


def read_bounded_bytes(path: str | Path, max_bytes: int) -> bytes:
    """Read at most ``max_bytes``; works for regular files, pipes and devices."""
    with Path(path).open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise InputError(f"{Path(path).name}: input exceeds the {max_bytes}-byte limit")
    return data


def decode_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        raise InputError(f"line {line}: invalid UTF-8") from None


def read_bounded_text(path: str | Path, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
    return decode_utf8(read_bounded_bytes(path, max_bytes))


# One physical line with its terminator (\r\n, \r or \n), or a final unterminated
# line. Iterating matches over the existing string avoids io.StringIO, whose
# internal UCS-4 buffer silently quadrupled peak memory (measured: 171 MB peak
# for 37 MB of CSV before this change).
_LINE = re.compile(r"[^\r\n]*(?:\r\n?|\n)|[^\r\n]+\Z")


def _bounded_lines(text: str, max_line_chars: int) -> Iterator[str]:
    for number, match in enumerate(_LINE.finditer(text), start=1):
        line = match.group()
        if len(line) > max_line_chars:
            raise InputError(f"line {number}: longer than the {max_line_chars}-character limit")
        yield line


class TelemetryFile:
    """A validated, immutable in-memory snapshot of one CSV file.

    ``records()`` re-parses the same snapshot deterministically, so callers can
    run a validation pass before emitting any output without a TOCTOU window.
    """

    def __init__(self, path: str | Path, limits: Limits = DEFAULT_LIMITS) -> None:
        data = read_bounded_bytes(path, limits.max_file_bytes)
        self.name = Path(path).name
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.limits = limits
        self._text = decode_utf8(data)
        header = self._header()
        self.index = ColumnIndex.from_header(header)
        self.columns = tuple(header)
        self.has_load = self.index.engine_load_pct is not None

    def _header(self) -> list[str]:
        reader = csv.reader(_bounded_lines(self._text, self.limits.max_line_chars), strict=True)
        try:
            header = next(reader, None)
        except csv.Error as exc:
            raise InputError(f"line 1: malformed CSV header: {exc}") from None
        if not header:
            raise InputError("empty input: a CSV header is required")
        return header

    def records(self) -> Iterator[Record]:
        reader = csv.reader(_bounded_lines(self._text, self.limits.max_line_chars), strict=True)
        rows = 0
        try:
            next(reader)  # header, validated in __init__
            for values in reader:
                if not values:  # blank line
                    continue
                rows += 1
                if rows > self.limits.max_rows:
                    raise InputError(f"{self.name}: more than {self.limits.max_rows} data rows")
                yield parse_row(values, reader.line_num, self.index)
        except csv.Error as exc:
            raise InputError(f"line {reader.line_num}: malformed CSV: {exc}") from None
