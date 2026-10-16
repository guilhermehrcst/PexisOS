"""v0.4 Lab A: offline, file-to-file replay of recorded classic-CAN frames through a DBC.

This module reads two local files (a DBC and a frame CSV) and assesses every
frame as ``decoded``, ``unknown_message`` or ``decode_error``. It never talks
to a CAN interface, a vehicle, an ECU or the network:

- nothing here creates a bus, imports ``can`` (python-can) or opens a device;
- the optional decoder (cantools) is imported only inside ``load_cantools_decoder``;
- ``no_device_access()`` installs a process audit hook that refuses socket
  creation, process creation, ``ctypes`` library loading and opening device
  paths while a replay runs, so a dependency that tried would fail the run
  (a second barrier that does not rely on reading the dependency's code).

Decoding a frame is not evidence that a reading is real, that a vehicle has a
defect, or that anything will fail. Signal names, units and scaling come only
from the user's DBC; unknown frames are never given a meaning.

Frame CSV contract (Lab A), exactly this header and no quoting:

    timestamp,arbitration_id,is_extended_id,data_hex

- ``timestamp``: RFC 3339 with seconds, optional fraction (<= 6 digits) and an
  explicit offset or ``Z``; years 1970-2199. Input order is kept as is.
- ``arbitration_id``: ``0x`` and 1-8 hex digits.
- ``is_extended_id``: ``0`` (11-bit, id <= 0x7FF) or ``1`` (29-bit, id <= 0x1FFFFFFF).
- ``data_hex``: 0-16 hex digits, even length (0-8 bytes, classic CAN).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import stat
import sys
import threading
import warnings
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Protocol

SCHEMA = "pexislink.can-replay.v1"
HEADER = "timestamp,arbitration_id,is_extended_id,data_hex"
STATUSES = ("decoded", "unknown_message", "decode_error")
NOTICE = ("Offline replay of recorded frames through a user-supplied DBC. Decoding is not evidence that a "
          "reading is real, that a vehicle is faulty, or that anything will fail. No device was accessed.")
CANTOOLS_VERSION = "44.1.0"  # the version the integration tests ran against (docs/CAN-REPLAY-LAB-A.md)

_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?"
    r"(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", re.ASCII)
_ARBITRATION_ID = re.compile(r"0x[0-9A-Fa-f]{1,8}", re.ASCII)
_HEX = re.compile(r"(?:[0-9A-Fa-f]{2}){0,8}", re.ASCII)
_HEX_ANY = re.compile(r"[0-9A-Fa-f]*", re.ASCII)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_STANDARD_ID = 0x7FF
_MAX_EXTENDED_ID = 0x1FFFFFFF
_CHUNK = 1 << 20
_MAX_LIMIT = 1 << 40  # 1 TiB / 10^12 rows: far above any real use, well inside Py_ssize_t for reads
_UNKNOWN_ID_CAP = 100_000
_MESSAGE_LINE = re.compile(r"[ \t]*BO_[ \t]")
_SIGNAL_LINE = re.compile(r"[ \t]*SG_[ \t]")


# --------------------------------------------------------------- errors

class ReplayError(Exception):
    """A gate failure: the run must not commit any output.

    ``code`` is stable and documented; the message never contains paths or payloads.
    """

    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line = line


class DecodeFailure(Exception):
    """A row-level decode problem; the row is reported as ``decode_error`` with ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------- limits

@dataclass(frozen=True, slots=True)
class ReplayLimits:
    max_frames_bytes: int = 256 * 1024 ** 2
    max_line_bytes: int = 256
    max_rows: int = 5_000_000
    max_dbc_bytes: int = 4 * 1024 ** 2
    max_messages: int = 10_000
    max_signals_per_message: int = 512  # cantools' strict parse is quadratic in signals per message
    max_signals: int = 50_000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_LIMIT:
                raise ValueError(f"{name} must be an integer between 1 and {_MAX_LIMIT}")


# --------------------------------------------------------------- frames

@dataclass(frozen=True, slots=True)
class Frame:
    line: int
    timestamp: str
    epoch_us: int
    arbitration_id: int
    is_extended_id: bool
    data: bytes


def parse_timestamp(text: str) -> int | None:
    """Epoch microseconds of a strict RFC 3339 timestamp, or ``None``."""
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    year = int(match.group(1))
    if not 1970 <= year <= 2199:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:  # e.g. February 30
        return None
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_frame(line_number: int, line: str) -> Frame:
    """Validate one data line against the closed contract (structural errors fail the run)."""
    if '"' in line:
        raise ReplayError("csv_quoting", "quoted fields are not part of the frame contract", line_number)
    fields = line.split(",")
    if len(fields) != 4:
        raise ReplayError("field_count", "a frame row must have exactly 4 fields", line_number)
    stamp, ident, extended, data_hex = fields
    epoch_us = parse_timestamp(stamp)
    if epoch_us is None:
        raise ReplayError("invalid_timestamp", "timestamp is not a supported RFC 3339 instant", line_number)
    if _ARBITRATION_ID.fullmatch(ident) is None:
        raise ReplayError("invalid_arbitration_id", "arbitration_id must be 0x and 1-8 hex digits", line_number)
    if extended not in ("0", "1"):
        raise ReplayError("invalid_extended_flag", "is_extended_id must be 0 or 1", line_number)
    value, is_extended = int(ident, 16), extended == "1"
    if value > (_MAX_EXTENDED_ID if is_extended else _MAX_STANDARD_ID):
        raise ReplayError("arbitration_id_out_of_range",
                          "arbitration_id exceeds the range of its frame format", line_number)
    if _HEX.fullmatch(data_hex) is None:
        if _HEX_ANY.fullmatch(data_hex) is not None and len(data_hex) % 2 == 0 and len(data_hex) > 16:
            raise ReplayError("unsupported_frame", "more than 8 data bytes (CAN FD) is not supported", line_number)
        raise ReplayError("invalid_data_hex", "data_hex must be an even number of hex digits", line_number)
    return Frame(line_number, stamp, epoch_us, value, is_extended, bytes.fromhex(data_hex))


# --------------------------------------------------------------- bounded local files

@dataclass(slots=True)
class InputFile:
    """A local regular file opened without following symlinks; hashed as it is read."""

    path: Path
    handle: BinaryIO
    info: os.stat_result
    max_bytes: int
    sha: Any
    bytes_read: int = 0

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self._account(data)
        return data

    def readline(self, limit: int) -> bytes:
        data = self.handle.readline(limit)
        self._account(data)
        return data

    def _account(self, data: bytes) -> None:
        self.sha.update(data)
        self.bytes_read += len(data)
        if self.bytes_read > self.max_bytes:
            raise ReplayError("input_changed" if self.info.st_size <= self.max_bytes else "input_too_large",
                              "input grew past its size limit while being read")

    def finish(self) -> str:
        """Hash the rest, then check the file did not change while read; return the SHA-256."""
        while self.read(_CHUNK):
            pass
        after = os.fstat(self.handle.fileno())
        same = self.bytes_read == self.info.st_size and all(
            getattr(after, key) == getattr(self.info, key) for key in ("st_size", "st_mtime_ns", "st_ctime_ns"))
        try:
            same = same and os.path.samestat(os.lstat(self.path), self.info)
        except OSError:
            same = False
        if not same:
            raise ReplayError("input_changed", "an input file changed while it was being read")
        return str(self.sha.hexdigest())


@contextmanager
def open_input(path: Path, max_bytes: int, role: str) -> Iterator[InputFile]:
    try:
        link = os.lstat(path)
    except FileNotFoundError:
        raise ReplayError("input_unreadable", f"the {role} file does not exist") from None
    except OSError:
        raise ReplayError("input_unreadable", f"the {role} file cannot be read") from None
    if stat.S_ISLNK(link.st_mode):
        raise ReplayError("input_symlink", f"the {role} file is a symbolic link (not followed)")
    if not stat.S_ISREG(link.st_mode):
        raise ReplayError("input_unreadable", f"the {role} file is not a regular file")
    if link.st_size > max_bytes:
        raise ReplayError("input_too_large", f"the {role} file is larger than its limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ReplayError("input_unreadable", f"the {role} file cannot be opened") from None
    info = os.fstat(descriptor)
    if not os.path.samestat(link, info) or not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ReplayError("input_changed", f"the {role} file was replaced while being opened")
    with os.fdopen(descriptor, "rb") as handle:
        yield InputFile(path, handle, info, max_bytes, hashlib.sha256())


def iter_frames(source: InputFile, limits: ReplayLimits, report: dict[str, Any]) -> Iterator[Frame]:
    """Stream validated frames. Structural problems raise ``ReplayError`` (fail closed)."""
    line_number = 0
    rows = 0
    while True:
        raw = source.readline(limits.max_line_bytes + 6)  # room for a BOM and CRLF around the content
        if not raw:
            break
        line_number += 1
        if line_number == 1 and raw.startswith(b"\xef\xbb\xbf"):
            report["utf8_bom"] = True
            raw = raw[3:]
        report["final_newline"] = raw.endswith(b"\n")
        body = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
        if len(body) > limits.max_line_bytes:  # content bytes, excluding BOM and line terminator
            raise ReplayError("line_too_long", "a line is longer than the line limit", line_number)
        if b"\r" in body or b"\x00" in body:
            raise ReplayError("invalid_line", "stray carriage return or NUL byte", line_number)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ReplayError("invalid_utf8", "the frame file is not valid UTF-8", line_number) from None
        if line_number == 1:
            if text != HEADER:
                raise ReplayError("invalid_header", f"the header must be exactly: {HEADER}", 1)
            continue
        rows += 1
        if rows > limits.max_rows:
            raise ReplayError("input_too_large", "more frame rows than the row limit", line_number)
        yield parse_frame(line_number, text)
    if line_number == 0:
        raise ReplayError("invalid_header", "the frame file is empty", 1)


# --------------------------------------------------------------- decoders

@dataclass(frozen=True, slots=True)
class SignalSpec:
    name: str
    unit: str | None


@dataclass(frozen=True, slots=True)
class MessageSpec:
    name: str
    frame_id: int
    is_extended_id: bool
    length: int
    signals: tuple[SignalSpec, ...]


class Decoder(Protocol):
    """A pure, local decoder: no I/O, no device, no network."""

    name: str
    version: str
    messages: Mapping[tuple[int, bool], MessageSpec]

    def decode(self, message: MessageSpec, data: bytes) -> Mapping[str, object]: ...


class _CantoolsDecoder:
    name = "cantools"

    def __init__(self, module: Any, database: Any, messages: dict[tuple[int, bool], MessageSpec]) -> None:
        self.version = str(getattr(module, "__version__", "unknown"))
        self._database = database
        self.messages: Mapping[tuple[int, bool], MessageSpec] = messages
        # Our own (frame_id, extended) key: cantools' get_message_by_frame_id() aliases an 11-bit and
        # a 29-bit message with the same numeric id (verified with 44.1.0, docs/CAN-REPLAY-LAB-A.md).
        self._native = {(m.frame_id, bool(m.is_extended_frame)): m for m in database.messages}

    def decode(self, message: MessageSpec, data: bytes) -> Mapping[str, object]:
        native = self._native[(message.frame_id, message.is_extended_id)]
        result = native.decode(data, decode_choices=False, scaling=True, decode_containers=False,
                               allow_truncated=False, allow_excess=False)
        if not isinstance(result, Mapping):
            raise DecodeFailure("unsupported_message_type")
        return result


def _prescan_dbc(text: str, limits: ReplayLimits) -> None:
    """Cheap line count before parsing: bounds cantools' quadratic strict-mode parse time."""
    messages = signals = in_message = 0
    for line in text.splitlines():
        if _MESSAGE_LINE.match(line):
            messages += 1
            in_message = 0
        elif _SIGNAL_LINE.match(line):
            signals += 1
            in_message += 1
            if in_message > limits.max_signals_per_message:
                raise ReplayError("invalid_dbc", "a DBC message has more signals than the per-message limit")
    if messages > limits.max_messages or signals > limits.max_signals:
        raise ReplayError("invalid_dbc", "the DBC has more messages or signals than the limits")


def load_cantools_decoder(dbc_text: str, limits: ReplayLimits) -> Decoder:
    """Parse a DBC with the optional cantools package. Raises ``ReplayError``."""
    _prescan_dbc(dbc_text, limits)
    try:
        import cantools  # type: ignore[import-not-found]  # optional; only here, never at package import time
        import cantools.database  # type: ignore[import-not-found]
    except ImportError:
        raise ReplayError("decoder_unavailable",
                          "the optional decoder is not installed: pip install 'pexislink[can]'") from None
    try:
        with _quiet("cantools"):  # its warnings name DBC messages (e.g. "Overwriting message ...")
            database = cantools.database.load_string(dbc_text, database_format="dbc", strict=True)
    except Exception:  # parser exceptions are many; their text may echo DBC content
        raise ReplayError("invalid_dbc", "the DBC could not be parsed (strict mode)") from None
    if not hasattr(database, "messages") or not hasattr(database, "nodes"):
        raise ReplayError("invalid_dbc", "the file is not a CAN database")
    messages: dict[tuple[int, bool], MessageSpec] = {}
    if len(database.messages) > limits.max_messages:
        raise ReplayError("invalid_dbc", "the DBC has more messages than the message limit")
    if not database.messages:
        raise ReplayError("invalid_dbc", "the DBC defines no message")
    if len({m.name for m in database.messages}) != len(database.messages):
        raise ReplayError("invalid_dbc", "two DBC messages share a name")
    for native in database.messages:
        key = (int(native.frame_id), bool(native.is_extended_frame))
        if key in messages:
            raise ReplayError("invalid_dbc", "two DBC messages share a frame id and format")
        if getattr(native, "is_fd", False) or not 0 <= native.length <= 8:
            raise ReplayError("invalid_dbc", "the DBC defines a CAN FD or longer-than-8-byte message")
        if getattr(native, "is_container", False):
            raise ReplayError("invalid_dbc", "container messages are not supported in Lab A")
        if key[0] > (_MAX_EXTENDED_ID if key[1] else _MAX_STANDARD_ID):
            raise ReplayError("invalid_dbc", "a DBC message id exceeds the range of its frame format")
        messages[key] = MessageSpec(str(native.name), key[0], key[1], int(native.length),
                                    tuple(SignalSpec(str(s.name), str(s.unit) if s.unit else None)
                                          for s in native.signals))
    return _CantoolsDecoder(cantools, database, messages)


@contextmanager
def _quiet(name: str) -> Iterator[None]:
    """Drop records of ``name`` and its child loggers (no output to stderr) during the block."""
    def drop(record: logging.LogRecord) -> bool:
        return not (record.name == name or record.name.startswith(name + "."))

    logger = logging.getLogger(name)
    saved = logger.propagate, logger.handlers[:]
    null = logging.NullHandler()
    logger.propagate, logger.handlers = False, [null]  # also bypasses handlers on child loggers' parents
    handlers = [logging.lastResort, *logging.getLogger().handlers]
    for handler in handlers:
        if handler is not None:
            handler.addFilter(drop)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.propagate, logger.handlers = saved
        for handler in handlers:
            if handler is not None:
                handler.removeFilter(drop)


# --------------------------------------------------------------- assessment

def assess(frame: Frame, index: int, decoder: Decoder, order: str) -> dict[str, Any]:
    """One output record per frame. No raw payload is written."""
    record: dict[str, Any] = {
        "index": index,
        "line": frame.line,
        "timestamp": frame.timestamp,
        "epoch_us": frame.epoch_us,
        "arbitration_id": f"0x{frame.arbitration_id:0{8 if frame.is_extended_id else 3}X}",
        "is_extended_id": frame.is_extended_id,
        "dlc": len(frame.data),
        "order": order,
    }
    message = decoder.messages.get((frame.arbitration_id, frame.is_extended_id))
    if message is None:
        record["status"] = "unknown_message"
        return record
    record["message"] = message.name
    if len(frame.data) != message.length:
        record.update(status="decode_error", error="length_mismatch", expected_dlc=message.length)
        return record
    try:
        values = decoder.decode(message, frame.data)
        signals = _signals(message, values)
    except DecodeFailure as failure:
        record.update(status="decode_error", error=failure.code)
        return record
    except Exception:  # never leak decoder exception text
        record.update(status="decode_error", error="decode_failed")
        return record
    record.update(status="decoded", signals=signals)
    return record


def _signals(message: MessageSpec, values: Mapping[str, object]) -> list[dict[str, Any]]:
    units = {signal.name: signal.unit for signal in message.signals}
    out = []
    for name in sorted(values):
        value = values[name]
        if name not in units:
            raise DecodeFailure("unexpected_signal")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DecodeFailure("unsupported_value_type")
        if isinstance(value, float) and not math.isfinite(value):
            raise DecodeFailure("nonfinite_value")
        if isinstance(value, int) and abs(value) > sys.float_info.max:
            raise DecodeFailure("value_out_of_range")  # would read as Infinity in IEEE-double JSON readers
        out.append({"name": name, "value": value, "unit": units[name]})
    return out


# --------------------------------------------------------------- safety guard

_BLOCKED_EVENTS = ("socket.__new__", "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                   "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
                   "os.forkpty", "os.startfile", "ctypes.dlopen", "ctypes.dlsym",
                   "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo")
_guard = {"active": False, "violations": 0}
_guard_lock = threading.Lock()  # serialize the process-global audit guard; concurrent/nested calls fail closed
_hook_installed = False


class SafetyViolation(RuntimeError):
    """Raised by the audit hook when replay code attempts device, network or process access."""


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _guard["active"]:
        return
    blocked = event.startswith(_BLOCKED_EVENTS)
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        blocked = _is_device(os.fsdecode(args[0]))
    if blocked:
        _guard["violations"] += 1
        raise SafetyViolation(event)


def _is_device(path: str) -> bool:
    """Refuse anything that is not a regular file or directory, and device trees by path.

    The path is made absolute against the current directory and resolved, so '//dev/x',
    '/./dev/x' or a chdir('/dev') do not bypass it. A path relative to a ``dir_fd`` cannot be
    seen by an audit hook (documented gap)."""
    try:
        resolved = os.path.realpath(path)
        mode = os.stat(resolved).st_mode
    except (OSError, ValueError):
        return False  # does not exist (yet): nothing to reach; creating files is not a device access
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
        return False
    return True


@contextmanager
def no_device_access() -> Iterator[None]:
    """Refuse audited network, device and process effects during one replay.

    The hook and counters are process-global. Protect BOTH guard activation and
    rejection of concurrent entry with the same lock, so a rejected contender
    cannot run between lock ownership and activation or lose its violation in
    an initialization reset. This is defense in depth, never a sandbox.
    """
    global _hook_installed
    with _guard_lock:
        if _guard["active"]:
            _guard["violations"] += 1
            raise ReplayError("safety_guard", "another replay guard is already active")
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True
        threads_before = {id(thread) for thread in threading.enumerate()}
        _guard.update(active=True, violations=0)

    pending: BaseException | None = None
    try:
        yield
    except BaseException as error:
        pending = error  # finish all checks before propagating any pending exception
    finally:
        with _guard_lock:
            try:
                # A late or swallowed concurrent entry fails the owner too.
                if any(id(thread) not in threads_before and thread.is_alive()
                       for thread in threading.enumerate()):
                    _guard["violations"] += 1
                violations = _guard["violations"]
            finally:
                _guard["active"] = False
        if violations:
            raise ReplayError(
                "safety_guard",
                "replay code attempted device, network, process, thread or nested replay activity",
            ) from pending
        if pending is not None:
            raise pending

# --------------------------------------------------------------- replay

def replay(frames: InputFile, decoder: Decoder, limits: ReplayLimits,
           write: Any, report: dict[str, Any]) -> Counter[str]:
    """Assess every frame in input order, calling ``write(record)`` once per frame."""
    statuses: Counter[str] = Counter({status: 0 for status in STATUSES})
    order: Counter[str] = Counter()
    unknown: set[tuple[int, bool]] = set()
    previous: Frame | None = None
    index = 0
    for frame in iter_frames(frames, limits, report):
        if previous is None:
            position = "first"
        elif frame.epoch_us > previous.epoch_us:
            position = "increasing"
        elif frame.epoch_us == previous.epoch_us:
            position = "equal"
        else:
            position = "backwards"
        order[position] += 1
        if previous is not None and (frame.epoch_us, frame.arbitration_id, frame.is_extended_id, frame.data) == (
                previous.epoch_us, previous.arbitration_id, previous.is_extended_id, previous.data):
            order["exact_duplicate_of_previous"] += 1
        record = assess(frame, index, decoder, position)
        statuses[record["status"]] += 1
        if record["status"] == "unknown_message" and len(unknown) < _UNKNOWN_ID_CAP:
            unknown.add((frame.arbitration_id, frame.is_extended_id))
        write(record)
        previous = frame
        index += 1
    report["rows"] = index
    report["order"] = dict(sorted(order.items()))
    report["distinct_unknown_ids"] = len(unknown)
    report["distinct_unknown_ids_capped"] = len(unknown) >= _UNKNOWN_ID_CAP
    return statuses


def package_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def dbc_schema(decoder: Decoder) -> list[dict[str, Any]]:
    """The data-dependent schema: message keys, names, lengths, signal names and units from the DBC."""
    return [{"arbitration_id": f"0x{m.frame_id:0{8 if m.is_extended_id else 3}X}", "is_extended_id": m.is_extended_id,
             "name": m.name, "length": m.length,
             "signals": [{"name": s.name, "unit": s.unit} for s in m.signals]}
            for _, m in sorted(decoder.messages.items())]
