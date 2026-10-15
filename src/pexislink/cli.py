"""Replay two CSV periods and emit machine-readable JSON Lines assessments."""

import argparse
import json
import sys

from .core import analyze, read_samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only telemetry anomaly research")
    parser.add_argument("--baseline", required=True, help="Earlier, known-reference CSV")
    parser.add_argument("--observations", required=True, help="Later telemetry CSV")
    parser.add_argument("--threshold-c", type=float, default=8.0)
    parser.add_argument("--min-baseline", type=int, default=5)
    parser.add_argument("--persistence", type=int, default=2)
    args = parser.parse_args(argv)
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
