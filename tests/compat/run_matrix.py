"""Run the OpenAI Agents SDK interoperability suite and report TEST_MATRIX outcomes.

    PYTHONPATH=src python tests/compat/run_matrix.py [--json OUT] [--require-all-pass]

Each matrix id T01..T15 is PASS when all of its tests pass, FAIL when any fails or errors,
and NOT_RUN when its tests were skipped (for example, the SDK is not installed). The report
records the exact interpreter, platform, package versions, git commit and command.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX = {
    "T01": "initialize", "T02": "discover", "T03": "observe_authorized", "T04": "hide_unauthorized",
    "T05": "reject_spoofed_principal", "T06": "expired_revoked", "T07": "schemas_and_cursor",
    "T08": "freshness_and_quality", "T09": "large_and_nonfinite", "T10": "error_sanitization",
    "T11": "isolation", "T12": "replay_determinism", "T13": "negative_hardware", "T14": "lifecycle",
    "T15": "no_runtime_dependency",
}


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--require-all-pass", action="store_true", help="exit 1 unless every id is PASS")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    suite = unittest.defaultTestLoader.loadTestsFromName("compat.test_openai_agents_stdio")
    ids: list[str] = []

    def walk(item):  # collect ids first: running a suite empties it
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())

    walk(suite)
    result = unittest.TestResult()
    suite.run(result)
    failed = {t.id() for t, _ in result.failures + result.errors}
    skipped = {t.id() for t, _ in result.skipped}
    outcomes: dict[str, list[str]] = defaultdict(list)
    for test_id in ids:
        match = re.search(r"\.test_(T\d\d)_", test_id)
        if match:
            outcomes[match.group(1)].append("fail" if test_id in failed else
                                            "skip" if test_id in skipped else "pass")
    matrix = {}
    for test_id, name in MATRIX.items():
        states = outcomes.get(test_id, [])
        status = ("NOT_RUN" if not states or all(s == "skip" for s in states) else
                  "FAIL" if "fail" in states else "PASS" if all(s == "pass" for s in states) else "FAIL")
        matrix[test_id] = {"name": name, "status": status, "tests": len(states),
                           "passed": states.count("pass"), "skipped": states.count("skip")}
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10).stdout.strip() or None
    except OSError:
        commit = None
    report = {
        "schema": "pexislink.compatibility.report.v0",
        "client": "openai-agents (agents.mcp.MCPServerStdio), no model calls",
        "server": "pexislink fabric mcp-stdio (+ tests/compat/_fixture_server.py)",
        "command": "PYTHONPATH=src python tests/compat/run_matrix.py",
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": f"{platform.system()}-{platform.machine()}",
                        "packages": {p: version(p) for p in ("openai-agents", "mcp", "mcp-types", "openai",
                                                              "jsonschema")},
                        "git_commit": commit},
        "summary": {s: sum(1 for m in matrix.values() if m["status"] == s) for s in ("PASS", "FAIL", "NOT_RUN")},
        "matrix": matrix,
        "failures": sorted(failed),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.require_all_pass and report["summary"]["PASS"] != len(MATRIX):
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
