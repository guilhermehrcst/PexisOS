"""Command-line entry point.

    pexislink detect --reference R.csv --observations O.csv [--summary S.json]
    pexislink synth --scenario NAME --seed N --out DIR
    pexislink benchmark [--profile quick|dev|full] --out DIR

The v0.1 invocation ``pexislink --baseline B --observations O`` still runs the
frozen v0.1 rule with its original output, plus a deprecation notice on stderr.

Exit codes: 0 success, 2 invalid input or usage (no assessments are emitted for
an invalid ``detect`` run), 1 output pipe closed early.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import __version__
from .config import DetectorConfig
from .core import analyze, read_samples
from .detect import STATUSES, assess_records
from .ingest import DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_VEHICLES, InputError, Limits, Record, TelemetryFile
from .reference import build_reference, check_chronology

COMMANDS = ("detect", "synth", "benchmark")
NOTICE = ("Research output. An anomaly is a deviation from this vehicle's own reference; "
          "it is not a diagnosed fault, a failure prediction or a safety assessment.")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if args else 2
    if args[0] == "--version":
        print(__version__)
        return 0
    if args[0] not in COMMANDS:
        return _legacy_v01(args)
    try:
        if args[0] == "detect":
            return _detect(args[1:])
        if args[0] == "synth":
            from .synth import cli_synth
            return cli_synth(args[1:])
        from .benchmark import cli_benchmark
        return cli_benchmark(args[1:])
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1


def _detect(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink detect",
                                     description="v0.2 read-only offline anomaly assessment (JSON Lines).")
    parser.add_argument("--reference", required=True, help="earlier reference-period CSV")
    parser.add_argument("--observations", required=True, help="later CSV to assess")
    parser.add_argument("--config", help="JSON file overriding DetectorConfig defaults (closed schema)")
    parser.add_argument("--summary", help="write a JSON run summary (provenance, counts) to this path")
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_BYTES >> 20)
    parser.add_argument("--max-vehicles", type=int, default=DEFAULT_MAX_VEHICLES)
    args = parser.parse_args(argv)
    try:
        config = DetectorConfig.from_json_file(args.config) if args.config else DetectorConfig()
        limits = Limits(max_file_bytes=args.max_file_mb << 20, max_vehicles=args.max_vehicles)
        reference = TelemetryFile(args.reference, limits)
        observations = TelemetryFile(args.observations, limits)
        use_load = config.use_load and reference.has_load and observations.has_load
        model = build_reference(reference.records(), config, use_load=use_load,
                                max_vehicles=limits.max_vehicles)
        # Validation pass: structure, limits and chronology are checked for the whole
        # file before the first assessment is written (fail closed, no partial output).
        check_chronology(model, _vehicle_limited(observations.records(), limits.max_vehicles))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    status_counts: Counter[str] = Counter({status: 0 for status in STATUSES})
    reason_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()
    write = sys.stdout.write
    for assessment in assess_records(model, observations.records(), max_vehicles=limits.max_vehicles):
        status_counts[assessment.status] += 1
        reason_counts.update(assessment.reasons)
        quality_counts.update(assessment.quality)
        write(json.dumps(assessment.to_json(), separators=(",", ":")) + "\n")
    sys.stdout.flush()

    if args.summary:
        summary = {
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
        try:
            write_json_atomic(args.summary, summary)
        except OSError as exc:
            print(f"error: cannot write summary: {exc}", file=sys.stderr)
            return 2
    return 0


def _vehicle_limited(records: Iterator[Record], max_vehicles: int) -> Iterator[Record]:
    seen: set[str] = set()
    for record in records:
        if record.vehicle_id is not None and record.vehicle_id not in seen:
            seen.add(record.vehicle_id)
            if len(seen) > max_vehicles:
                raise InputError(f"line {record.line}: more than {max_vehicles} vehicles")
        yield record


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """Write JSON via a temporary file and rename, so readers never see a partial file."""
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _legacy_v01(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink",
                                     description="Read-only telemetry anomaly research (legacy v0.1 rule)")
    parser.add_argument("--baseline", required=True, help="Earlier, known-reference CSV")
    parser.add_argument("--observations", required=True, help="Later telemetry CSV")
    parser.add_argument("--threshold-c", type=float, default=8.0)
    parser.add_argument("--min-baseline", type=int, default=5)
    parser.add_argument("--persistence", type=int, default=2)
    args = parser.parse_args(argv)
    print("pexislink: deprecated v0.1 invocation; it runs the frozen v0.1 rule with known "
          "defects (docs/AUDIT-v0.1.md). Use 'pexislink detect'.", file=sys.stderr)
    try:
        assessments = analyze(
            read_samples(args.baseline),
            read_samples(args.observations),
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
