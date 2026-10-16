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
    11  safety_guard (replay code attempted device, network or process access)

Commit protocol. Records and summary are written to temporary files next to
their destinations and fsync'ed. Then any existing summary is removed, the
records file is renamed into place, and the summary is renamed last. The
summary carries the SHA-256 and count of the records: a records file without
a matching summary is an interrupted run and must not be used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
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
    "input_changed": 10, "safety_guard": 11,
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
    args = parser.parse_args(list(argv))
    try:
        limits = ReplayLimits(max_frames_bytes=args.max_frames_mb << 20, max_line_bytes=args.max_line_bytes,
                              max_rows=args.max_rows, max_dbc_bytes=args.max_dbc_kb << 10,
                              max_messages=args.max_messages, max_signals_per_message=args.max_signals_per_message,
                              max_signals=args.max_signals)
        dbc, frames, out, summary = (Path(p) for p in (args.dbc, args.frames, args.out, args.summary))
        _check_destinations(out, summary, (dbc, frames))
    except ValueError as exc:
        return _fail("usage", str(exc))
    command = ["pexislink", "lab", "can-replay", "--dbc", "<dbc>", "--frames", "<frames>", "--out", "<out>",
               "--summary", "<summary>", "--max-frames-mb", str(args.max_frames_mb), "--max-line-bytes",
               str(args.max_line_bytes), "--max-rows", str(args.max_rows), "--max-dbc-kb", str(args.max_dbc_kb),
               "--max-messages", str(args.max_messages), "--max-signals-per-message",
               str(args.max_signals_per_message), "--max-signals", str(args.max_signals)]
    return _run(dbc, frames, out, summary, limits, command, decoder_factory)


def _run(dbc: Path, frames: Path, out: Path, summary: Path, limits: ReplayLimits, command: list[str],
         decoder_factory: DecoderFactory) -> int:
    try:
        records = _Pending(out)
    except OSError as exc:
        return _fail("output_write_error", f"cannot create the records file: {exc.strerror or 'error'}")
    try:
        manifest_file = _Pending(summary)
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
                       "max_signals_per_message": limits.max_signals_per_message},
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
        if summary.exists():
            summary.unlink()  # a stale summary must never pair with new records
            # Persist the marker removal BEFORE replacing records, including
            # when the two output files live on different filesystems/dirs.
            # Otherwise a power loss may resurrect a stale valid-looking
            # summary after records.commit() has become durable.
            _fsync_directory(summary)
        records.commit()
        manifest_file.commit()
    except OSError as exc:  # records may already be in place without a summary: an interrupted run
        records.discard()
        manifest_file.discard()
        return _fail("output_write_error", f"cannot commit output: {exc.strerror or 'error'}")
    except BaseException:
        records.discard()
        manifest_file.discard()
        raise
    print(f"can-replay: {sum(statuses.values())} frame(s): "
          + ", ".join(f"{s} {statuses[s]}" for s in STATUSES), file=sys.stderr)
    return 0


def _check_destinations(out: Path, summary: Path, inputs: tuple[Path, ...]) -> None:
    def same(a: Path, b: Path) -> bool:
        if os.path.realpath(a) == os.path.realpath(b):
            return True
        try:
            return os.path.samestat(os.stat(a), os.stat(b))
        except OSError:
            return False

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
    """A temporary file next to its destination; renamed into place on commit."""

    def __init__(self, target: Path) -> None:
        self.target = target
        try:  # keep an existing file's permissions (the summary is removed before its rename)
            self.mode: int | None = stat.S_IMODE(os.stat(target).st_mode)
        except FileNotFoundError:
            self.mode = None
        descriptor, name = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(target)),
                                            prefix=".pexislink-", suffix=".tmp")  # short: long targets fit
        self.temporary = Path(name)
        self.handle: IO[bytes] = os.fdopen(descriptor, "wb")
        self.sha = hashlib.sha256()
        self.count = 0
        self.bytes = 0

    def write_record(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
        self.handle.write(line)
        self.sha.update(line)
        self.count += 1
        self.bytes += len(line)

    def write_text(self, text: str) -> None:
        self.handle.write(text.encode("ascii"))

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
        _fsync_directory(self.target)

    def discard(self) -> None:
        if not self.handle.closed:
            self.handle.close()
        self.temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    except OSError:
        return  # not supported (e.g. Windows): the rename itself is still atomic
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fail(code: str, message: str) -> int:
    print(f"error: {code}: {message}", file=sys.stderr)
    return EXIT_CODES.get(code, CORRUPT_FRAMES)
