"""Command-line entry point.

    pexislink detect --reference R.csv --observations O.csv [--summary S.json]
    pexislink synth --scenario NAME --seed N --out DIR
    pexislink benchmark [--profile quick|dev|full] --out DIR
    pexislink dataset preflight --source component-x-v3 --input-dir DIR --out MANIFEST.json
    pexislink fabric demo | mcp-stdio --principal ID [--policy FILE]   (experimental, read-only)
    pexislink lab can-replay --dbc FILE --frames FILE --out RECORDS.jsonl --summary SUMMARY.json

The v0.1 invocation ``pexislink --baseline B --observations O`` still runs the
frozen v0.1 rule with its original output, plus a deprecation notice on stderr.

Exit codes: 0 success; 2 invalid input or usage (``detect`` then emits no
assessments: the input is fully validated and the summary destination is
prepared before the first line is written); 1 output could not be written
(for example, the pipe was closed early or the device is full).

``dataset preflight`` is read-only and offline. Exit codes: 0 every expected
file present and structurally clean; 3 at least one blocking issue; 4 no
blocking issue but expected files missing; 2 invalid usage or unusable input
directory (no manifest written); 1 the manifest could not be written.

``lab can-replay`` is offline and file-to-file (no CAN interface, no
transmission); its exit codes are documented in ``pexislink/lab_cli.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import IO, Any, Iterator, Sequence

from . import __version__
from .config import DetectorConfig
from .core import analyze, read_samples
from .detect import STATUSES, assess_records
from .ingest import DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_VEHICLES, InputError, Limits, Record, TelemetryFile
from .reference import build_reference

COMMANDS = ("detect", "synth", "benchmark", "dataset", "fabric", "lab")
NOTICE = ("Research output. An anomaly is a deviation from this vehicle's own reference; "
          "it is not a diagnosed fault, a failure prediction or a safety assessment.")
MAX_FILE_MB = 4096


def main(argv: Sequence[str] | None = None) -> int:
    try:
        code = _dispatch(list(sys.argv[1:] if argv is None else argv))
        sys.stdout.flush()  # report buffered-output errors here, not at interpreter exit
    except BrokenPipeError:
        _discard_stdout()
        return 1
    except OSError as exc:
        print(f"error: cannot write output: {exc}", file=sys.stderr)
        _discard_stdout()
        return 1
    return code


def _dispatch(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if args else 2
    if args[0] == "--version":
        print(__version__)
        return 0
    if args[0] not in COMMANDS:
        return _legacy_v01(args)
    if args[0] == "detect":
        return _detect(args[1:])
    if args[0] == "dataset":
        return _dataset(args[1:])
    if args[0] == "fabric":
        from .fabric.cli import main as fabric_main
        return fabric_main(args[1:])
    if args[0] == "lab":
        from .lab_cli import main as lab_main
        return lab_main(args[1:])
    if args[0] == "synth":
        from .synth import cli_synth
        return cli_synth(args[1:])
    from .benchmark import cli_benchmark
    return cli_benchmark(args[1:])


def _discard_stdout() -> None:
    """After a failed write, point stdout at the null device. Otherwise the
    interpreter flushes the leftover buffer again at exit, fails again, and
    exits with status 120 instead of the documented 1."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):  # stdout is not an OS file: nothing left to flush there
        pass


def _file_mb(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_FILE_MB:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_FILE_MB}")
    return value


def _vehicles(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 1_000_000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000000")
    return value


def _detect(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink detect",
                                     description="v0.2 read-only offline anomaly assessment (JSON Lines).")
    parser.add_argument("--reference", required=True, help="earlier reference-period CSV")
    parser.add_argument("--observations", required=True, help="later CSV to assess")
    parser.add_argument("--config", help="JSON file overriding DetectorConfig defaults (closed schema)")
    parser.add_argument("--summary", help="write a JSON run summary (provenance, counts) to this path")
    parser.add_argument("--max-file-mb", type=_file_mb, default=DEFAULT_MAX_FILE_BYTES >> 20)
    parser.add_argument("--max-vehicles", type=_vehicles, default=DEFAULT_MAX_VEHICLES)
    args = parser.parse_args(argv)
    summary_file: _PendingFile | None = None
    try:
        config = DetectorConfig.from_json_file(args.config) if args.config else DetectorConfig()
        limits = Limits(max_file_bytes=args.max_file_mb << 20, max_vehicles=args.max_vehicles)
        reference = TelemetryFile(args.reference, limits)
        observations = TelemetryFile(args.observations, limits)
        use_load = config.use_load and reference.has_load and observations.has_load
        model = build_reference(reference.records(), config, use_load=use_load,
                                max_vehicles=limits.max_vehicles, max_contexts=limits.max_contexts)
        # Validation pass: structure and limits are checked for the whole file before
        # the first assessment is written (fail closed, no partial output).
        for _ in _vehicle_limited(observations.records(), limits.max_vehicles):
            pass
        if args.summary:
            summary_file = _PendingFile(args.summary, forbidden=(args.reference, args.observations, args.config))
    except (ValueError, OSError, RecursionError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    status_counts: Counter[str] = Counter({status: 0 for status in STATUSES})
    reason_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    try:
        write = sys.stdout.write
        for assessment in assess_records(model, observations.records(), max_vehicles=limits.max_vehicles):
            status_counts[assessment.status] += 1
            reason_counts.update(assessment.reasons)
            quality_counts.update(assessment.quality)
            write(json.dumps(assessment.to_json(), separators=(",", ":"), allow_nan=False) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as exc:
        if summary_file:
            summary_file.discard()
        print(f"error: cannot write assessments: {exc}", file=sys.stderr)
        _discard_stdout()
        return 1

    overlaps = reason_counts.get("reference_overlap", 0)
    if overlaps:
        print(f"warning: {overlaps} observation row(s) are not after their vehicle's reference period "
              "and were not assessed (reason reference_overlap)", file=sys.stderr)
    if summary_file:
        try:
            summary_file.commit(_summary(config, reference, observations, model,
                                         status_counts, reason_counts, quality_counts))
        except (OSError, ValueError) as exc:
            print(f"error: cannot write summary: {exc}", file=sys.stderr)
            return 2
    return 0


def _dataset(argv: list[str]) -> int:
    from .componentx import SOURCE_ID, PreflightLimits, run_preflight

    parser = argparse.ArgumentParser(prog="pexislink dataset",
                                     description="Read-only, offline preflight of locally supplied dataset files.")
    sub = parser.add_subparsers(dest="action", required=True)
    pre = sub.add_parser("preflight", help="validate local files and write a provenance manifest (JSON)")
    pre.add_argument("--source", required=True, choices=[SOURCE_ID])
    pre.add_argument("--input-dir", required=True, help="local directory holding the publisher's files")
    pre.add_argument("--out", required=True, help="manifest path (outside the input directory)")
    defaults = PreflightLimits()
    pre.add_argument("--max-file-mb", type=int, default=defaults.max_file_bytes >> 20)
    pre.add_argument("--max-line-bytes", type=int, default=defaults.max_line_bytes)
    pre.add_argument("--max-rows", type=int, default=defaults.max_rows)
    pre.add_argument("--max-vehicles", type=int, default=defaults.max_vehicles)
    pre.add_argument("--max-columns", type=int, default=defaults.max_columns)
    pre.add_argument("--max-dir-entries", type=int, default=defaults.max_dir_entries)
    pre.add_argument("--max-categories", type=int, default=defaults.max_categories)
    args = parser.parse_args(argv)
    # Messages never echo local paths (they may name people or vehicles); errno text only.
    try:
        limits = PreflightLimits(max_file_bytes=args.max_file_mb << 20,
                                 max_line_bytes=args.max_line_bytes, max_rows=args.max_rows,
                                 max_vehicles=args.max_vehicles, max_columns=args.max_columns,
                                 max_dir_entries=args.max_dir_entries, max_categories=args.max_categories)
        input_dir = Path(args.input_dir)
        if not input_dir.is_dir():
            raise ValueError("--input-dir does not exist or is not a directory")
        if _inside(Path(args.out), input_dir):
            raise ValueError("refusing to write the manifest inside the input directory (it is read-only input)")
    except (ValueError, OSError, RuntimeError) as exc:  # RuntimeError: symlink loop in resolve() on 3.11
        print(f"error: {_no_path(exc)}", file=sys.stderr)
        return 2
    try:
        manifest_file = _PendingFile(args.out, forbidden=())
    except ValueError:  # the destination is not a regular file: a usage error
        print("error: --out exists and is not a regular file", file=sys.stderr)
        return 2
    except OSError as exc:  # missing or unwritable parent: the manifest cannot be written
        print(f"error: cannot write manifest: {_no_path(exc)}", file=sys.stderr)
        return 1
    command = ["pexislink", "dataset", "preflight", "--source", args.source, "--input-dir", "<input-dir>",
               "--out", "<out>", "--max-file-mb", str(args.max_file_mb), "--max-line-bytes",
               str(args.max_line_bytes), "--max-rows", str(args.max_rows), "--max-vehicles",
               str(args.max_vehicles), "--max-columns", str(args.max_columns), "--max-dir-entries",
               str(args.max_dir_entries), "--max-categories", str(args.max_categories)]
    started = time.perf_counter()
    try:
        manifest = run_preflight(input_dir, limits, command)
    except (ValueError, OSError, RecursionError, MemoryError) as exc:
        manifest_file.discard()
        print(f"error: {_no_path(exc)}", file=sys.stderr)
        return 2
    except BaseException:  # e.g. Ctrl-C: leave no temporary file behind
        manifest_file.discard()
        raise
    try:
        manifest_file.commit(manifest, sort_keys=True)
    except (OSError, ValueError) as exc:
        print(f"error: cannot write manifest: {_no_path(exc)}", file=sys.stderr)
        return 1
    overall = manifest["overall"]
    print(f"preflight: {overall['status']}; {len(overall['blocking_issues'])} blocking issue(s); "
          f"{len(overall['missing_expected_files'])} expected file(s) missing; "
          f"{time.perf_counter() - started:.1f} s; peak RSS {_peak_rss_mb()} MB (not in the manifest)",
          file=sys.stderr)
    return {"pass": 0, "fail": 3, "incomplete": 4}[overall["status"]]


def _no_path(exc: BaseException) -> str:
    """Error text without file names: OSError messages embed the path."""
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    if isinstance(exc, RuntimeError):
        return "symbolic link loop or unresolvable path"
    return str(exc)


def _inside(out: Path, directory: Path) -> bool:
    """True if ``out`` would land in ``directory`` or below. Compares device and inode
    of each existing parent, so case-insensitive or aliased paths cannot bypass it."""
    target = os.stat(directory)
    for parent in Path(os.path.realpath(out)).parents:  # realpath: no RuntimeError on loops (3.11)
        try:
            if os.path.samestat(os.stat(parent), target):
                return True
        except OSError:
            continue
    return False


def _peak_rss_mb() -> float | str:
    try:
        import resource
    except ImportError:  # not available on Windows
        return "unknown"
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)  # macOS bytes, Linux KiB


def _summary(config: DetectorConfig, reference: TelemetryFile, observations: TelemetryFile,
             model: Any, status_counts: Counter[str], reason_counts: Counter[str],
             quality_counts: Counter[str]) -> dict[str, Any]:
    return {
        "schema": "pexislink.summary.v2",
        "pexislink_version": __version__,
        "notice": NOTICE,
        "config": config.to_dict(),
        "inputs": {name: {"file": table.name, "sha256": table.sha256, "columns": list(table.columns)}
                   for name, table in (("reference", reference), ("observations", observations))},
        "reference_model": model.summary(),
        "observations": {
            "rows": sum(status_counts.values()),
            "status_counts": dict(status_counts),
            "reason_counts": dict(sorted(reason_counts.items())),
            "quality_counts": dict(sorted(quality_counts.items())),
        },
    }


def _vehicle_limited(records: Iterator[Record], max_vehicles: int) -> Iterator[Record]:
    seen: set[str] = set()
    for record in records:
        if record.vehicle_id is not None and record.vehicle_id not in seen:
            seen.add(record.vehicle_id)
            if len(seen) > max_vehicles:
                raise InputError(f"line {record.line}: more than {max_vehicles} vehicles")
        yield record


class _PendingFile:
    """A temporary file created next to its destination before any output, so a
    bad destination fails the run early; renamed into place on commit."""

    def __init__(self, path: str, *, forbidden: Sequence[str | None]) -> None:
        self.target = Path(path)
        if self.target.exists() and not self.target.is_file():
            # a directory would fail only at the final rename; a device node (/dev/null) would be replaced
            raise ValueError(f"summary destination exists and is not a regular file: {path}")
        for other in forbidden:
            if other and self.target.exists() and Path(other).exists() and self.target.samefile(other):
                raise ValueError(f"refusing to overwrite an input file with the summary: {path}")
        descriptor, self.temporary = tempfile.mkstemp(dir=self.target.parent, prefix=f".{self.target.name}.",
                                                      suffix=".tmp")
        self.handle: IO[str] = os.fdopen(descriptor, "w", encoding="utf-8")

    def commit(self, payload: Any, *, sort_keys: bool = False) -> None:
        try:
            with self.handle:
                json.dump(payload, self.handle, indent=2, allow_nan=False, sort_keys=sort_keys)
                self.handle.write("\n")
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(self.temporary, 0o666 & ~mask)  # mkstemp creates 0600
            os.replace(self.temporary, self.target)
        except BaseException:
            self.discard()
            raise

    def discard(self) -> None:
        self.handle.close()
        Path(self.temporary).unlink(missing_ok=True)


def _legacy_v01(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink",
                                     description="Read-only telemetry anomaly research (legacy v0.1 rule)")
    parser.add_argument("--baseline", required=True, help="Earlier, known-reference CSV")
    parser.add_argument("--observations", required=True, help="Later telemetry CSV")
    parser.add_argument("--threshold-c", type=float, default=8.0)
    parser.add_argument("--min-baseline", type=int, default=5)
    parser.add_argument("--persistence", type=int, default=2)
    parser.add_argument("--max-file-mb", type=_file_mb, default=DEFAULT_MAX_FILE_BYTES >> 20)
    args = parser.parse_args(argv)
    print("pexislink: deprecated v0.1 invocation; it runs the frozen v0.1 rule with known "
          "defects (docs/AUDIT-v0.1.md). Use 'pexislink detect'.", file=sys.stderr)
    max_bytes = args.max_file_mb << 20
    try:
        assessments = analyze(
            read_samples(args.baseline, max_bytes=max_bytes),
            read_samples(args.observations, max_bytes=max_bytes),
            threshold_c=args.threshold_c,
            min_baseline=args.min_baseline,
            persistence=args.persistence,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for assessment in assessments:
        sample = assessment.sample
        print(json.dumps({
            "timestamp": sample.timestamp.isoformat(),
            "vehicle_id": sample.vehicle_id,
            "status": assessment.status,
            "coolant_temp_c": sample.coolant_temp_c,
            "reference_c": assessment.reference_c,
            "delta_c": assessment.delta_c,
        }, separators=(",", ":")))
    return 0
