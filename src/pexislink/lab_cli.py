"""``pexislink lab can-replay``: command-line wiring for the offline CAN replay lab.

    pexislink lab can-replay --dbc FILE --frames FILE --out RECORDS.jsonl --summary SUMMARY.json

Exit codes (stable; messages on stderr never contain paths or payload bytes):

     0  success: one record per frame, then the summary (the commit marker)
     1  output_write_error
     2  usage error (arguments, limits, an output that aliases an input or the other output)
     3  decoder_unavailable (pip install 'pexislink[can]')
     4  invalid_dbc
     5  corrupt_frames (encoding, header, quoting, field syntax, timestamp, id, extended flag, hex)
     6  unsupported_frame (more than 8 data bytes: CAN FD)
     7  input_unreadable (missing, not a regular file, permission)
     8  input_symlink
     9  input_too_large (file bytes, line length, row count)
    10  input_changed (grew, was rewritten or replaced while read)
    11  safety_guard (replay code attempted device, network or process access, the audit hook was
        refused, or an open descriptor refers to a device, kernel control file or non-local socket)
    12  output_too_large (records or summary would exceed --max-output-mb; checked before each write)

Commit protocol. Records and summary are written to temporary files in the
resolved directory of their destinations and fsync'ed. Then any existing summary
is removed and its directory fsync'ed, the records file is renamed into place and
its directory fsync'ed, and the summary is renamed last and its directory
fsync'ed. Every directory fsync is required on POSIX: a failure fails the run, and
a summary already renamed by the failing run is removed again. The summary carries
the SHA-256 and count of the records: a records file without a matching summary is
an interrupted run and must not be used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from . import __version__
from .canreplay import (
    NOTICE,
    SCHEMA,
    STATUSES,
    Decoder,
    ReplayError,
    ReplayLimits,
    dbc_schema,
    load_cantools_decoder,
    no_device_access,
    open_input,
    package_fingerprint,
    replay,
)

EXIT_CODES = {
    "output_write_error": 1, "usage": 2, "decoder_unavailable": 3, "invalid_dbc": 4,
    "unsupported_frame": 6, "input_unreadable": 7, "input_symlink": 8, "input_too_large": 9, "line_too_long": 9,
    "input_changed": 10, "safety_guard": 11, "output_too_large": 12,
}
CORRUPT_FRAMES = 5  # every other ReplayError code comes from the frame contract

DecoderFactory = Callable[[str, ReplayLimits], Decoder]


def main(argv: Sequence[str], decoder_factory: DecoderFactory = load_cantools_decoder) -> int:
    parser = argparse.ArgumentParser(prog="pexislink lab", description="Offline research labs (no device access).")
    sub = parser.add_subparsers(dest="lab", required=True)
    can = sub.add_parser("can-replay", help="decode recorded classic-CAN frames through a local DBC")
    can.add_argument("--dbc", required=True, help="local DBC file (UTF-8)")
    can.add_argument("--frames", required=True, help="local frame CSV: " + "timestamp,arbitration_id,is_extended_id,data_hex")
    can.add_argument("--out", required=True, help="JSON Lines records, one per frame")
    can.add_argument("--summary", required=True, help="JSON summary, written last (the commit marker)")
    defaults = ReplayLimits()
    can.add_argument("--max-frames-mb", type=int, default=defaults.max_frames_bytes >> 20)
    can.add_argument("--max-line-bytes", type=int, default=defaults.max_line_bytes)
    can.add_argument("--max-rows", type=int, default=defaults.max_rows)
    can.add_argument("--max-dbc-kb", type=int, default=defaults.max_dbc_bytes >> 10)
    can.add_argument("--max-messages", type=int, default=defaults.max_messages)
    can.add_argument("--max-signals-per-message", type=int, default=defaults.max_signals_per_message)
    can.add_argument("--max-signals", type=int, default=defaults.max_signals)
    can.add_argument("--max-output-mb", type=int, default=defaults.max_output_bytes >> 20)
    args = parser.parse_args(list(argv))
    try:
        limits = ReplayLimits(max_frames_bytes=args.max_frames_mb << 20, max_line_bytes=args.max_line_bytes,
                              max_rows=args.max_rows, max_dbc_bytes=args.max_dbc_kb << 10,
                              max_messages=args.max_messages, max_signals_per_message=args.max_signals_per_message,
                              max_signals=args.max_signals, max_output_bytes=args.max_output_mb << 20)
        dbc, frames, out, summary = (Path(p) for p in (args.dbc, args.frames, args.out, args.summary))
        _check_destinations(out, summary, (dbc, frames))
    except ValueError as exc:
        return _fail("usage", str(exc))
    command = ["pexislink", "lab", "can-replay", "--dbc", "<dbc>", "--frames", "<frames>", "--out", "<out>",
               "--summary", "<summary>", "--max-frames-mb", str(args.max_frames_mb), "--max-line-bytes",
               str(args.max_line_bytes), "--max-rows", str(args.max_rows), "--max-dbc-kb", str(args.max_dbc_kb),
               "--max-messages", str(args.max_messages), "--max-signals-per-message",
               str(args.max_signals_per_message), "--max-signals", str(args.max_signals),
               "--max-output-mb", str(args.max_output_mb)]
    return _run(dbc, frames, out, summary, limits, command, decoder_factory)


def _run(dbc: Path, frames: Path, out: Path, summary: Path, limits: ReplayLimits, command: list[str],
         decoder_factory: DecoderFactory) -> int:
    try:
        records = _Pending(out, limits.max_output_bytes)
    except OSError as exc:
        return _fail("output_write_error", f"cannot create the records file: {exc.strerror or 'error'}")
    try:
        manifest_file = _Pending(summary, limits.max_output_bytes)
    except OSError as exc:
        records.discard()
        return _fail("output_write_error", f"cannot create the summary file: {exc.strerror or 'error'}")
    try:
        with no_device_access():
            with open_input(dbc, limits.max_dbc_bytes, "DBC") as source:
                raw = source.read(limits.max_dbc_bytes + 1)  # never more than the cap, even if it grew
                dbc_sha = source.finish()
            try:
                text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
            except UnicodeDecodeError:
                raise ReplayError("invalid_dbc", "the DBC is not valid UTF-8") from None
            decoder = decoder_factory(text, limits)
            report: dict[str, Any] = {"utf8_bom": False, "final_newline": True}
            with open_input(frames, limits.max_frames_bytes, "frame") as source:
                statuses = replay(source, decoder, limits, records.write_record, report)
                frames_sha = source.finish()
                frames_bytes = source.bytes_read
        records.close()
        payload = {
            "schema": SCHEMA,
            "notice": NOTICE,
            "tool": {"pexislink_version": __version__, "package_sources_sha256": package_fingerprint(),
                     "python": sys.version.split()[0]},
            "decoder": {"name": decoder.name, "version": decoder.version},
            "command": command,
            "limits": {"max_frames_bytes": limits.max_frames_bytes, "max_line_bytes": limits.max_line_bytes,
                       "max_rows": limits.max_rows, "max_dbc_bytes": limits.max_dbc_bytes,
                       "max_messages": limits.max_messages, "max_signals": limits.max_signals,
                       "max_signals_per_message": limits.max_signals_per_message,
                       "max_output_bytes": limits.max_output_bytes},
            "inputs": {"dbc": {"sha256": dbc_sha, "bytes": len(raw)},
                       "frames": {"sha256": frames_sha, "bytes": frames_bytes, "utf8_bom": report["utf8_bom"],
                                  "final_newline": report["final_newline"]}},
            "dbc_schema": dbc_schema(decoder),
            "frames": {"rows": report["rows"], "status_counts": {s: statuses[s] for s in STATUSES},
                       "timestamp_order": report["order"], "distinct_unknown_ids": report["distinct_unknown_ids"],
                       "distinct_unknown_ids_capped": report["distinct_unknown_ids_capped"]},
            "records": {"count": records.count, "sha256": records.sha.hexdigest(), "bytes": records.bytes},
        }
        manifest_file.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
        manifest_file.close()
    except ReplayError as error:
        records.discard()
        manifest_file.discard()
        where = f" (line {error.line})" if error.line else ""
        return _fail(error.code, f"{error}{where}")
    except OSError as exc:
        records.discard()
        manifest_file.discard()
        return _fail("output_write_error", f"cannot write output: {exc.strerror or 'error'}")
    except BaseException:  # Ctrl-C or a bug: never leave temporary files or a partial commit
        records.discard()
        manifest_file.discard()
        raise
    try:
        if os.path.lexists(manifest_file.target):
            os.unlink(manifest_file.target)  # a stale summary must never pair with new records
            # Persist the marker removal BEFORE replacing records, including
            # when the two output files live on different filesystems/dirs.
            # Otherwise a power loss may resurrect a stale valid-looking
            # summary after records.commit() has become durable.
            _fsync_directory(manifest_file.directory)
        records.commit()  # rename + required directory fsync: records durable before the marker
        manifest_file.commit()
    except OSError as exc:  # records may already be in place without a summary: an interrupted run
        records.discard()
        manifest_file.withdraw()  # a marker the failing run renamed must not claim success
        return _fail("output_write_error", f"cannot commit output: {exc.strerror or 'error'}")
    except BaseException:
        records.discard()
        manifest_file.withdraw()
        raise
    print(f"can-replay: {sum(statuses.values())} frame(s): "
          + ", ".join(f"{s} {statuses[s]}" for s in STATUSES), file=sys.stderr)
    return 0


def _name_key(path: Path) -> tuple[str, str]:
    """(resolved parent, case- and normalization-folded name): equal keys may be one directory entry.

    Case-insensitive or normalizing filesystems (macOS APFS/HFS+, Windows, some mounts) map such
    names to the same entry, and that cannot be checked reliably before the files exist, so equal
    keys are rejected on every filesystem."""
    parent = os.path.realpath(os.path.dirname(path) or ".")
    return parent, unicodedata.normalize("NFC", os.path.basename(path)).casefold()


def _check_destinations(out: Path, summary: Path, inputs: tuple[Path, ...]) -> None:
    def same(a: Path, b: Path) -> bool:
        if os.path.realpath(a) == os.path.realpath(b) or _name_key(a) == _name_key(b):
            return True
        try:
            return os.path.samestat(os.stat(a), os.stat(b))
        except OSError:
            return False

    for destination in (out, summary):
        if os.path.basename(destination) in ("", ".", ".."):
            raise ValueError("an output path must name a file")

    if same(out, summary):
        raise ValueError("--out and --summary must be different files")
    for destination in (out, summary):
        for source in inputs:
            if same(destination, source):
                raise ValueError("an output path must not be an input file")
        if os.path.lexists(destination):
            if os.path.islink(destination) or not os.path.isfile(destination):
                raise ValueError("an existing output must be a regular file (not a link, directory or device)")


class _Pending:
    """A temporary file in its destination's resolved directory; renamed into place on commit.

    The destination directory is resolved once, the way the kernel resolves it (symlinks before
    ``..``), and that one directory is used for the temporary file, the rename and the fsync."""

    def __init__(self, target: Path, max_bytes: int) -> None:
        self.directory = Path(os.path.realpath(os.path.dirname(target) or "."))
        self.target = self.directory / os.path.basename(target)
        self.max_bytes = max_bytes
        try:  # keep an existing file's permissions (the summary is removed before its rename)
            self.mode: int | None = stat.S_IMODE(os.stat(self.target).st_mode)
        except FileNotFoundError:
            self.mode = None
        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix=".pexislink-",
                                            suffix=".tmp")  # short: long targets fit
        self.temporary = Path(name)
        self.handle: IO[bytes] = os.fdopen(descriptor, "wb")
        self.sha = hashlib.sha256()
        self.count = 0
        self.bytes = 0
        self.committed = False

    def _reserve(self, size: int) -> None:
        if self.bytes + size > self.max_bytes:  # before the write: never grows past the budget
            raise ReplayError("output_too_large", "output would exceed --max-output-mb")

    def write_record(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
        self._reserve(len(line))
        self.handle.write(line)
        self.sha.update(line)
        self.count += 1
        self.bytes += len(line)

    def write_text(self, text: str) -> None:
        data = text.encode("ascii")
        self._reserve(len(data))
        self.handle.write(data)
        self.bytes += len(data)

    def close(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()

    def commit(self) -> None:
        mode = self.mode
        if mode is None:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask  # mkstemp creates 0600
        os.chmod(self.temporary, mode)
        os.replace(self.temporary, self.target)
        self.committed = True
        _fsync_directory(self.directory)

    def discard(self) -> None:
        if not self.handle.closed:
            self.handle.close()
        self.temporary.unlink(missing_ok=True)

    def withdraw(self) -> None:
        """Discard, and remove the destination too if this run already renamed it into place."""
        self.discard()
        if self.committed:
            try:
                os.unlink(self.target)
                _fsync_directory(self.directory)
            except OSError:
                pass  # best effort: the run already reports failure


def _fsync_directory(directory: Path) -> None:
    """Make a rename or unlink in ``directory`` durable. Required: any failure propagates.

    POSIX only. Windows cannot open a directory for fsync; it is not a supported platform for the
    lab (not run), so there the call is skipped and durability is not claimed."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fail(code: str, message: str) -> int:
    print(f"error: {code}: {message}", file=sys.stderr)
    return EXIT_CODES.get(code, CORRUPT_FRAMES)
