"""Read-only, streaming preflight for locally supplied Scania Component X v3 files.

This module answers one question: *are the files in a local directory what the
publisher describes, and are they structurally fit for later, vehicle-isolated
research?* It trains nothing, predicts nothing and derives no feature. It never
touches the network (enforced by ``tests/test_architecture.py``) and never
writes into the input directory.

Evidence discipline. Every statement about the dataset carries one of four
statuses:

- ``publisher_claim``: stated by the publisher or the dataset descriptor, as
  recorded in ``SOURCE`` with its evidence; *not* checked by this tool.
- ``verified_local``: computed by this tool from the bytes of a local file.
- ``not_available``: the expected file is not in the input directory.
- ``not_checked``: the check was deliberately skipped or impossible.

Outcome files (``train_tte.csv``, ``validation_labels.csv``,
``test_labels.csv``) hold future-event information. They are read
*metadata-only*: header line, byte length, SHA-256 and physical line count. No
outcome value is ever decoded, so nothing downstream can use labels as inputs.

Column semantics are not interpreted. Anonymous columns are reported by name
only; "candidate decreases" are counted without assuming any column is a
counter. ``time_step`` is anonymised relative time with no known unit.

Privacy. The manifest contains no row values, no vehicle identifiers and no
local paths: only counts, line numbers, column names, byte lengths and hashes.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import platform
import re
import stat
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from . import __version__

SOURCE_ID = "component-x-v3"
SCHEMA = "pexislink.preflight.component-x.v1"
NOTICE = ("Data-quality preflight only. No model, prediction, risk score or label-derived feature is "
          "produced. Anonymous columns are not interpreted. A repair record is not a proven failure.")

# --------------------------------------------------------------- evidence

_CATALOG = "https://researchdata.se/en/catalogue/dataset/2024-34/3"
_SEARCH = ("web-search index excerpt of the publisher page (2026-10-15); the page itself could not "
           "be fetched from this environment (HTTP 403 / DNS failure), so this is not a primary read")
_DESCRIPTOR = "dataset descriptor, Kharazian et al., Scientific Data 12, 493 (2025), as cited in issue #4"
_ISSUE = "owner's source research in issue #4 (2026-10-15), not re-verified here"

SOURCE: dict[str, Any] = {
    "name": {"value": "SCANIA Component X Dataset", "status": "publisher_claim", "evidence": _SEARCH},
    "version": {"value": "3", "status": "publisher_claim", "evidence": _SEARCH},
    "doi": {"value": "10.5878/bnh5-ka77", "status": "publisher_claim", "evidence": _SEARCH},
    "catalog_url": {"value": _CATALOG, "status": "publisher_claim", "evidence": "plan 0002"},
    "descriptor_doi": {"value": "10.1038/s41597-025-04802-6", "status": "publisher_claim",
                       "evidence": "plan 0002; issue #4"},
    "previous_version_doi": {"value": "10.5878/jvb5-d390", "status": "publisher_claim",
                             "evidence": f"{_ISSUE}; also cited as version 2 on the publisher's download page "
                                         "(web-search excerpt)"},
    "total_size": {"value": "11 files, about 1.54 GiB", "status": "publisher_claim", "evidence": _SEARCH},
}

LICENSE: dict[str, Any] = {
    "value": "CC BY 4.0",
    "status": "publisher_claim",
    "verification": "unverified",
    "evidence": (f"{_ISSUE}. The licence field of version 3 could not be read from the primary source in "
                 "this environment; read it at the catalog URL before any download or redistribution."),
    "attribution": ("If the publisher licence is confirmed as CC BY 4.0: cite the dataset (DOI) and the "
                    "descriptor, and indicate modifications. Raw files are never committed to this repository."),
}


@dataclass(frozen=True, slots=True)
class ExpectedFile:
    """One file the publisher is described as distributing. All fields are claims."""

    split: str
    role: str  # operational | specifications | outcome
    evidence: str
    rows: int | None = None
    vehicles: int | None = None
    columns: int | None = None
    column_names: tuple[str, ...] = ()
    bytes_min: int | None = None  # inclusive range implied by a rounded size display
    bytes_max: int | None = None


def _kib(display: float) -> tuple[int, int]:
    """Byte range of a size shown rounded to two decimals in KiB."""
    return math.ceil((display - 0.005) * 1024), math.floor((display + 0.005) * 1024)


def _mib(display: float) -> tuple[int, int]:
    return math.ceil((display - 0.005) * 1024 ** 2), math.floor((display + 0.005) * 1024 ** 2)


EXPECTED: dict[str, ExpectedFile] = {
    "train_operational_readouts.csv": ExpectedFile(
        "train", "operational", f"{_DESCRIPTOR}; file name from {_ISSUE}",
        rows=1_122_452, vehicles=23_550, columns=107),
    "validation_operational_readouts.csv": ExpectedFile(
        "validation", "operational", f"{_DESCRIPTOR}; file name by analogy, unverified",
        rows=196_227, vehicles=5_046),
    "test_operational_readouts.csv": ExpectedFile(
        "test", "operational", f"{_DESCRIPTOR}; file name from a secondary review, unverified",
        rows=198_140, vehicles=5_045),
    "train_specifications.csv": ExpectedFile(
        "train", "specifications", f"{_SEARCH}; {_DESCRIPTOR}",
        rows=23_550, bytes_min=_mib(1.03)[0], bytes_max=_mib(1.03)[1]),
    "validation_specifications.csv": ExpectedFile(
        "validation", "specifications", "file name by analogy, unverified"),
    "test_specifications.csv": ExpectedFile(
        "test", "specifications", "file name by analogy, unverified"),
    "train_tte.csv": ExpectedFile(
        "train", "outcome", f"{_SEARCH}; {_DESCRIPTOR}",
        rows=23_550, column_names=("length_of_study_time_step", "in_study_repair"),
        bytes_min=_kib(337.32)[0], bytes_max=_kib(337.32)[1]),
    "validation_labels.csv": ExpectedFile(
        "validation", "outcome", f"{_SEARCH}; {_DESCRIPTOR}",
        rows=5_046, column_names=("class_label",),
        bytes_min=_kib(37.83)[0], bytes_max=_kib(37.83)[1]),
    "test_labels.csv": ExpectedFile(
        "test", "outcome", f"{_SEARCH} (byte count shown as 38,682)",
        rows=5_045, column_names=("class_label",), bytes_min=38_682, bytes_max=38_682),
}
SPLITS = ("train", "validation", "test")
KEY_COLUMNS = {"operational": ("vehicle_id", "time_step"), "specifications": ("vehicle_id",)}

# --------------------------------------------------------------- limits

@dataclass(frozen=True, slots=True)
class PreflightLimits:
    """Explicit resource bounds; exceeding one fails the affected file, never silently."""

    max_file_bytes: int = 2 * 1024 ** 3
    max_line_bytes: int = 64 * 1024
    max_rows: int = 5_000_000
    max_vehicles: int = 100_000
    max_columns: int = 1024
    max_dir_entries: int = 10_000
    max_categories: int = 256  # distinct values tracked per specification column

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


# --------------------------------------------------------------- parsing

_COLUMN_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}", re.ASCII)
_VEHICLE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}", re.ASCII)
_DIGITS = re.compile(r"[+-]?[0-9]+", re.ASCII)
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}", re.ASCII)
_EXACT_FLOAT_INT = 2 ** 53
_MAX_NUMBER_CHARS = 64
_SAMPLE_LINES = 5
_CHUNK = 1 << 20
_MAGIC = ((b"\x1f\x8b", "application/gzip"), (b"PK\x03\x04", "application/zip"),
          (b"\xfd7zXZ\x00", "application/x-xz"), (b"BZh", "application/x-bzip2"),
          (b"(\xb5/\xfd", "application/zstd"))


class _FileFailure(Exception):
    """A file-level structural problem: the file's content cannot be trusted further."""

    def __init__(self, code: str, line: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.line = line


def parse_number(cell: str) -> tuple[float | None, str | None]:
    """Strict ASCII decimal parse. Returns ``(value, None)`` or ``(None, issue)``.

    ``float()`` alone accepts '1_000', surrounding blanks, non-ASCII digits and
    'nan'/'inf'; none of those are accepted here, and nothing is coerced to 0.
    """
    if cell == "":
        return None, "missing"
    if len(cell) > _MAX_NUMBER_CHARS:
        return None, "number_too_long"
    try:
        value = float(cell)
    except ValueError:
        return None, "non_numeric"
    if not cell.isascii() or "_" in cell or cell != cell.strip(" \t\r\n\f\v"):
        return None, "non_numeric"
    if not math.isfinite(value):  # 'nan', 'inf' and overflow such as '1e400'
        return None, "nonfinite"
    return value, None


def precision_lost(cell: str, value: float) -> bool:
    """True when an integer literal cannot be represented exactly as a float."""
    return abs(value) >= _EXACT_FLOAT_INT and _DIGITS.fullmatch(cell) is not None and int(cell) != int(value)


class _HashedFile:
    """Single pass over a binary file: SHA-256 and byte count of every byte,
    bounded physical lines, strict UTF-8. Nothing is materialised."""

    def __init__(self, handle: BinaryIO, max_line_bytes: int) -> None:
        self.handle = handle
        self.max_line_bytes = max_line_bytes
        self.sha = hashlib.sha256()
        self.bytes = 0
        self.lines = 0
        self.bom = False
        self.final_newline = True

    def _consume(self, raw: bytes) -> None:
        self.sha.update(raw)
        self.bytes += len(raw)

    def raw_lines(self) -> Iterator[bytes]:
        while True:
            raw = self.handle.readline(self.max_line_bytes + 1)
            if not raw:
                return
            self._consume(raw)
            self.lines += 1
            if len(raw) > self.max_line_bytes:
                raise _FileFailure("line_too_long", self.lines)
            if not raw.endswith(b"\n"):
                self.final_newline = False
            yield raw

    def text_lines(self) -> Iterator[str]:
        for raw in self.raw_lines():
            if self.lines == 1 and raw.startswith(b"\xef\xbb\xbf"):
                self.bom = True
                raw = raw[3:]
            if b"\x00" in raw:
                raise _FileFailure("nul_byte", self.lines)
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _FileFailure("invalid_utf8", self.lines) from None

    def drain(self) -> None:
        """Hash the rest of the file without decoding it; count newlines."""
        while chunk := self.handle.read(_CHUNK):
            self._consume(chunk)
            self.lines += chunk.count(b"\n")
            self.final_newline = chunk.endswith(b"\n")


@dataclass(slots=True)
class _Issues:
    counts: Counter[str] = field(default_factory=Counter)
    lines: dict[str, list[int]] = field(default_factory=dict)

    def add(self, code: str, line: int) -> None:
        self.counts[code] += 1
        sample = self.lines.setdefault(code, [])
        if len(sample) < _SAMPLE_LINES:
            sample.append(line)

    def to_json(self) -> dict[str, Any]:
        return {code: {"count": self.counts[code], "first_lines": self.lines[code]}
                for code in sorted(self.counts)}


@dataclass(slots=True)
class _Column:
    missing: int = 0
    non_numeric: int = 0
    nonfinite: int = 0
    number_too_long: int = 0
    precision_lost: int = 0
    candidate_decreases: int = 0
    minimum: float | None = None
    maximum: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"missing": self.missing, "non_numeric": self.non_numeric, "nonfinite": self.nonfinite,
                "number_too_long": self.number_too_long, "integer_precision_lost": self.precision_lost,
                "candidate_decreases": self.candidate_decreases, "min": self.minimum, "max": self.maximum}


def _header(rows: Iterator[list[str]], role: str, limits: PreflightLimits) -> list[str]:
    try:
        header = next(rows)
    except StopIteration:
        raise _FileFailure("empty_file", 1) from None
    if len(header) > limits.max_columns:
        raise _FileFailure("too_many_columns", 1)
    if not header or any(_COLUMN_NAME.fullmatch(name) is None for name in header):
        raise _FileFailure("unsupported_column_name", 1)
    if len(set(header)) != len(header):
        raise _FileFailure("duplicate_column_name", 1)
    if any(key not in header for key in KEY_COLUMNS.get(role, ())):
        raise _FileFailure("missing_key_column", 1)
    return header


def _csv_rows(source: _HashedFile) -> Iterator[list[str]]:
    reader = csv.reader(source.text_lines(), strict=True)
    try:
        for row in reader:
            yield row
    except csv.Error:
        raise _FileFailure("csv_syntax", source.lines) from None


def _delta_bucket(delta: float) -> str:
    exponent = math.frexp(delta)[1]  # delta in [2**(e-1), 2**e)
    return f"[2^{exponent - 1},2^{exponent})"


def _check_operational(source: _HashedFile, limits: PreflightLimits, report: dict[str, Any],
                       vehicles_out: set[str]) -> None:
    rows = _csv_rows(source)
    header = _header(rows, "operational", limits)
    report["header"] = {"columns": len(header), "names": header}
    width = len(header)
    vid_at, time_at = header.index("vehicle_id"), header.index("time_step")
    features = [i for i in range(width) if i not in (vid_at, time_at)]
    columns = {header[i]: _Column() for i in features}
    stats = [columns[header[i]] for i in features]
    issues = _Issues()
    last_time: dict[str, float] = {}
    deltas: Counter[str] = Counter()
    current: str | None = None
    block_last: list[float | None] = [None] * len(features)
    accepted = rejected = backwards = equal = reappear = multiline = 0
    all_integer_ids = True
    previous_line = source.lines
    count = 0
    for row in rows:
        count += 1
        line = source.lines
        if count > limits.max_rows:
            raise _FileFailure("row_limit_exceeded", line)
        if line - previous_line > 1:
            multiline += 1
            issues.add("multiline_record", line)
        previous_line = line
        if len(row) != width:
            rejected += 1
            issues.add("wrong_field_count", line)
            continue
        vid = row[vid_at]
        if _VEHICLE_ID.fullmatch(vid) is None:
            rejected += 1
            issues.add("invalid_vehicle_id", line)
            continue
        time_value, problem = parse_number(row[time_at])
        if time_value is None:
            rejected += 1
            issues.add(f"time_step_{problem}", line)
            continue
        accepted += 1
        if vid != current:
            if vid in last_time:
                reappear += 1
                issues.add("vehicle_rows_not_contiguous", line)
            elif len(last_time) >= limits.max_vehicles:
                raise _FileFailure("vehicle_limit_exceeded", line)
            else:
                last_time[vid] = -math.inf
                all_integer_ids = all_integer_ids and vid.isdigit()
            current = vid
            block_last = [None] * len(features)
        previous_time = last_time[vid]
        if time_value > previous_time:
            if previous_time != -math.inf:
                deltas[_delta_bucket(time_value - previous_time)] += 1
        elif time_value == previous_time:
            equal += 1
            issues.add("duplicate_vehicle_time_step", line)
        else:
            backwards += 1
            issues.add("time_step_backwards", line)
        last_time[vid] = max(previous_time, time_value)
        for slot, index in enumerate(features):
            cell = row[index]
            column = stats[slot]
            if cell == "":
                column.missing += 1
                continue
            value, problem = parse_number(cell)
            if value is None:
                setattr(column, problem or "non_numeric", getattr(column, problem or "non_numeric") + 1)
                continue
            if value >= _EXACT_FLOAT_INT or value <= -_EXACT_FLOAT_INT:
                column.precision_lost += precision_lost(cell, value)
            if column.minimum is None or value < column.minimum:
                column.minimum = value
            if column.maximum is None or value > column.maximum:
                column.maximum = value
            before = block_last[slot]
            if before is not None and value < before:
                column.candidate_decreases += 1
            block_last[slot] = value
    report["rows"] = {
        "data_rows": count, "accepted": accepted, "rejected": rejected,
        "issues": issues.to_json(), "multiline_records": multiline,
    }
    report["vehicles"] = {"distinct": len(last_time), "all_ids_decimal_digits": all_integer_ids if last_time else None,
                          "non_contiguous_reappearances": reappear}
    report["time_step"] = {
        "unit": "anonymised relative time; no physical unit is assumed",
        "backwards_steps": backwards,
        "duplicate_keys": {"adjacent_equal": equal,
                           "completeness": "exhaustive" if backwards == 0 else "lower_bound"},
        "positive_delta_histogram": dict(sorted(deltas.items(), key=lambda item: _bucket_order(item[0]))),
        "order_scope": "per vehicle, in file order, without sorting",
    }
    report["features"] = {
        "count": len(features),
        "semantics": "not interpreted; candidate_decreases counts value decreases within a vehicle's "
                     "contiguous rows and does not assume any column is a counter",
        "columns": {name: column.to_json() for name, column in columns.items()},
    }
    vehicles_out.update(last_time)


def _bucket_order(label: str) -> int:
    return int(label[3:label.index(",")])


def _check_specifications(source: _HashedFile, limits: PreflightLimits, report: dict[str, Any],
                          vehicles_out: set[str]) -> None:
    rows = _csv_rows(source)
    header = _header(rows, "specifications", limits)
    report["header"] = {"columns": len(header), "names": header}
    width = len(header)
    vid_at = header.index("vehicle_id")
    others = [i for i in range(width) if i != vid_at]
    missing = Counter[str]()
    values: dict[str, set[str]] = {header[i]: set() for i in others}
    over_cap: set[str] = set()
    issues = _Issues()
    seen: set[str] = set()
    accepted = rejected = count = 0
    for row in rows:
        count += 1
        line = source.lines
        if count > limits.max_rows:
            raise _FileFailure("row_limit_exceeded", line)
        if len(row) != width:
            rejected += 1
            issues.add("wrong_field_count", line)
            continue
        vid = row[vid_at]
        if _VEHICLE_ID.fullmatch(vid) is None:
            rejected += 1
            issues.add("invalid_vehicle_id", line)
            continue
        if vid in seen:
            rejected += 1
            issues.add("duplicate_vehicle_id", line)
            continue
        if len(seen) >= limits.max_vehicles:
            raise _FileFailure("vehicle_limit_exceeded", line)
        seen.add(vid)
        accepted += 1
        for index in others:
            name, cell = header[index], row[index]
            if cell == "":
                missing[name] += 1
            elif name not in over_cap:
                values[name].add(cell)
                if len(values[name]) > limits.max_categories:
                    over_cap.add(name)
                    values[name].clear()
    report["rows"] = {"data_rows": count, "accepted": accepted, "rejected": rejected, "issues": issues.to_json()}
    report["vehicles"] = {"distinct": len(seen)}
    report["columns"] = {header[i]: {"missing": missing[header[i]],
                                     "distinct_values": (f"more than {limits.max_categories}"
                                                         if header[i] in over_cap else len(values[header[i]]))}
                         for i in others}
    vehicles_out.update(seen)


def _check_outcome(source: _HashedFile, report: dict[str, Any]) -> None:
    """Metadata only: decode the header line, then hash the rest undecoded."""
    first = next(source.raw_lines(), None)
    if first is None:
        raise _FileFailure("empty_file", 1)
    if first.startswith(b"\xef\xbb\xbf"):
        source.bom = True
        first = first[3:]
    try:
        names = next(csv.reader([first.decode("utf-8")], strict=True), [])
    except (UnicodeDecodeError, csv.Error):
        raise _FileFailure("invalid_header", 1) from None
    if not names or any(_COLUMN_NAME.fullmatch(name) is None for name in names):
        raise _FileFailure("unsupported_column_name", 1)
    report["header"] = {"columns": len(names), "names": names}
    source.drain()
    data_lines = source.lines - 1
    report["physical_lines_after_header"] = data_lines
    report["label_policy"] = ("outcome values were not decoded; only the header, byte length, SHA-256 "
                              "and physical line count were read")


def _sniff(handle: BinaryIO) -> str:
    head = handle.read(8)
    handle.seek(0)
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    return "text/csv"


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise _FileFailure("not_a_regular_file")
    return descriptor, info


def check_file(path: Path, name: str, limits: PreflightLimits, vehicles_out: set[str]) -> dict[str, Any]:
    """Preflight one local file. Never raises for content problems: they are reported."""
    expected = EXPECTED[name]
    report: dict[str, Any] = {"status": "verified_local", "split": expected.split, "role": expected.role,
                              "check": "fail", "fatal": None}
    try:
        descriptor, info = _open_regular(path)
    except _FileFailure as failure:
        report.update(status="not_checked", fatal={"code": failure.code, "line": None})
        return report
    except OSError as exc:
        report.update(status="not_checked", fatal={"code": "unreadable", "line": None, "errno": exc.errno})
        return report
    with os.fdopen(descriptor, "rb") as handle:
        report["bytes"] = info.st_size
        if info.st_size > limits.max_file_bytes:
            report.update(sha256={"status": "not_checked", "reason": "file larger than max_file_bytes"},
                          fatal={"code": "file_too_large", "line": None})
            report["publisher_claims"] = _claims(expected, report)
            return report
        source = _HashedFile(handle, limits.max_line_bytes)
        report["content_type"] = _sniff(handle)
        try:
            if report["content_type"] != "text/csv":
                raise _FileFailure("unsupported_compression", None)
            if expected.role == "operational":
                _check_operational(source, limits, report, vehicles_out)
            elif expected.role == "specifications":
                _check_specifications(source, limits, report, vehicles_out)
            else:
                _check_outcome(source, report)
            report["check"] = "metadata_only" if expected.role == "outcome" else "pass"
        except _FileFailure as failure:
            report["fatal"] = {"code": failure.code, "line": failure.line}
            vehicles_out.clear()  # a failed file contributes no vehicle set to split checks
        source.drain()
        report["sha256"] = source.sha.hexdigest()
        report["bytes_read"] = source.bytes
        report["physical_lines"] = source.lines
        report["utf8_bom"] = source.bom
        report["final_newline"] = source.final_newline
        if source.bytes != info.st_size or os.fstat(handle.fileno()).st_size != info.st_size:
            report["check"] = "fail"
            report["fatal"] = {"code": "file_changed_during_read", "line": None}
            vehicles_out.clear()
        elif report["check"] == "pass" and report["rows"]["rejected"]:
            report["check"] = "fail"
    report["publisher_claims"] = _claims(expected, report)
    return report


def _compare(claim: int | None, observed: int | None) -> dict[str, Any] | None:
    if claim is None:
        return None
    if observed is None:
        return {"publisher_claim": claim, "verified_local": "not_checked", "match": "not_checked"}
    return {"publisher_claim": claim, "verified_local": observed, "match": claim == observed}


def _claims(expected: ExpectedFile, report: dict[str, Any]) -> dict[str, Any]:
    present = report.get("status") == "verified_local"
    complete = present and report.get("fatal") is None  # counts of a failed file are partial
    header = report.get("header") or {}
    rows_observed = (report.get("rows") or {}).get("data_rows") if complete else None
    if complete and expected.role == "outcome":
        lines = report["physical_lines_after_header"]
        rows_observed = lines if report["final_newline"] else lines + 1
    claims: dict[str, Any] = {"evidence": expected.evidence}
    if not present:
        claims["note"] = "file not available locally; nothing compared"
    for key, claim, observed in (
        ("data_rows", expected.rows, rows_observed),
        ("distinct_vehicles", expected.vehicles,
         (report.get("vehicles") or {}).get("distinct") if complete else None),
        ("columns", expected.columns, header.get("columns") if present else None),
    ):
        compared = _compare(claim, observed)
        if compared:
            claims[key] = compared
    if expected.column_names:
        names = header.get("names")
        claims["column_names_include"] = {
            "publisher_claim": list(expected.column_names),
            "verified_local": "not_checked" if names is None else [n for n in expected.column_names if n in names],
            "match": "not_checked" if names is None else all(n in names for n in expected.column_names),
        }
    if expected.bytes_min is not None:
        size = report.get("bytes") if present else None
        claims["bytes"] = {
            "publisher_claim": (expected.bytes_min if expected.bytes_min == expected.bytes_max
                                else {"min": expected.bytes_min, "max": expected.bytes_max}),
            "verified_local": "not_checked" if size is None else size,
            "match": "not_checked" if size is None else expected.bytes_min <= size <= expected.bytes_max,
        }
    return claims


# --------------------------------------------------------------- directory

def _split_checks(sets: dict[str, set[str] | None]) -> dict[str, Any]:
    def ids(split: str, role: str) -> set[str] | None:
        return sets.get(f"{split}:{role}")

    overlap: dict[str, Any] = {}
    for index, first in enumerate(SPLITS):
        for second in SPLITS[index + 1:]:
            a, b = ids(first, "operational"), ids(second, "operational")
            key = f"{first}__{second}"
            overlap[key] = ("not_checked" if a is None or b is None
                            else {"status": "verified_local", "shared_vehicle_ids": len(a & b)})
    coverage: dict[str, Any] = {}
    for split in SPLITS:
        ops, specs = ids(split, "operational"), ids(split, "specifications")
        coverage[split] = ("not_checked" if ops is None or specs is None else {
            "status": "verified_local",
            "only_in_operational": len(ops - specs),
            "only_in_specifications": len(specs - ops),
        })
    return {
        "operational_vehicle_overlap_between_splits": overlap,
        "operational_vs_specifications_coverage": coverage,
        "outcome_files": "not_checked: outcome files are read metadata-only, so vehicle coverage of labels is "
                         "deliberately not computed in Stage A",
        "note": "counts only; no vehicle identifier is written. The publisher split is not assumed correct.",
    }


def tool_provenance() -> dict[str, Any]:
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return {
        "pexislink_version": __version__,
        "package_sources_sha256": digest.hexdigest(),
        "git_head": _git_head(package),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
    }


def _git_head(package: Path) -> dict[str, Any]:
    """Read the checkout's HEAD from files (no subprocess). Does not detect
    uncommitted changes; ``package_sources_sha256`` covers the code actually run."""
    for root in (package, *package.parents):
        git = root / ".git"
        if not git.is_dir():
            continue
        try:
            head = (git / "HEAD").read_text(encoding="ascii").strip()
            if not head.startswith("ref: "):
                return {"status": "verified_local", "sha": head, "ref": None}
            ref = head[5:]
            loose = git / ref
            if loose.is_file():
                return {"status": "verified_local", "sha": loose.read_text(encoding="ascii").strip(), "ref": ref}
            packed = git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="ascii").splitlines():
                    sha, _, name = line.partition(" ")
                    if name == ref:
                        return {"status": "verified_local", "sha": sha, "ref": ref}
        except (OSError, UnicodeDecodeError):
            break
        break
    return {"status": "not_available", "sha": None, "ref": None}


def run_preflight(input_dir: Path, limits: PreflightLimits, command: list[str]) -> dict[str, Any]:
    """Preflight every expected file present in ``input_dir`` (top level only)."""
    entries: list[os.DirEntry[str]] = []
    with os.scandir(input_dir) as scan:
        for entry in scan:
            entries.append(entry)
            if len(entries) > limits.max_dir_entries:
                raise ValueError(f"input directory has more than {limits.max_dir_entries} entries")
    present = {entry.name: entry for entry in entries}
    files: dict[str, Any] = {}
    vehicle_sets: dict[str, set[str] | None] = {}
    for name in sorted(EXPECTED):
        expected = EXPECTED[name]
        found = present.get(name)
        if found is None:
            files[name] = {"status": "not_available", "split": expected.split, "role": expected.role,
                           "publisher_claims": _claims(expected, {"status": "not_available"})}
            continue
        vehicles: set[str] = set()
        files[name] = check_file(Path(found.path), name, limits, vehicles)
        if expected.role != "outcome":
            ok = files[name].get("fatal") is None and files[name]["status"] == "verified_local"
            vehicle_sets[f"{expected.split}:{expected.role}"] = vehicles if ok else None
    unrecognized = sorted(name for name in present if name not in EXPECTED)
    blocking = sorted(f"{name}: {report['fatal']['code']}" for name, report in files.items()
                      if report.get("fatal"))
    blocking += sorted(f"{name}: rejected_rows" for name, report in files.items()
                       if report.get("fatal") is None and (report.get("rows") or {}).get("rejected"))
    missing = sorted(name for name, report in files.items() if report["status"] == "not_available")
    status = "fail" if blocking else ("incomplete" if missing else "pass")
    return {
        "schema": SCHEMA,
        "notice": NOTICE,
        "source_id": SOURCE_ID,
        "source": SOURCE,
        "license": LICENSE,
        "tool": tool_provenance(),
        "command": command,
        "limits": asdict(limits),
        "input_directory": {
            "path": "not recorded (privacy)",
            "entries": len(entries),
            "unrecognized_entries": sorted(n if _SAFE_FILE_NAME.fullmatch(n) else "<name withheld>"
                                           for n in unrecognized),
        },
        "files": files,
        "split_checks": _split_checks(vehicle_sets),
        "overall": {"status": status, "blocking_issues": blocking, "missing_expected_files": missing,
                    "meaning": "pass: every expected file present and structurally clean; incomplete: no "
                               "blocking issue but expected files missing; fail: at least one blocking issue. "
                               "None of these is a statement about predictive value."},
    }
