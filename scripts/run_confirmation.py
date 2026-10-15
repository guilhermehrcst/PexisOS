"""Confirmatory benchmark run on seeds never used before (ADR 0001 §8).

Runs the unchanged benchmark harness with the ``full`` profile's protocol on
seeds 301-310 and writes the same three files as ``pexislink benchmark``.

Usage: PYTHONPATH=src python scripts/run_confirmation.py [OUT_DIR]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pexislink.benchmark import DEV_SEEDS, PROFILES, TEST_SEEDS, Profile, render_markdown, run_benchmark

CONFIRMATION_SEEDS = tuple(range(301, 311))


def main(argv: list[str]) -> int:
    if set(CONFIRMATION_SEEDS) & (set(DEV_SEEDS) | set(TEST_SEEDS)):
        raise SystemExit("confirmation seeds must be disjoint from the development and test seeds")
    full = PROFILES["full"]
    profile = Profile("confirm", "confirmation", CONFIRMATION_SEEDS, full.reference_days, full.eval_days)
    folder = Path(argv[0] if argv else "reports/benchmark-confirm")
    results, provenance = run_benchmark(profile, progress=lambda message: print(message, file=sys.stderr))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True, allow_nan=False) + "\n",
                                         encoding="utf-8")
    (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    (folder / "report.md").write_text(render_markdown(results, provenance), encoding="utf-8")
    for key, hypothesis in results["hypotheses"].items():
        print(f"{key}: {hypothesis['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
