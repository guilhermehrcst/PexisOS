"""Run every client interoperability suite and write one machine-readable multi-client report.

    PYTHONPATH=src python tests/compat/run_clients.py [--json OUT] [--require CLIENT ...]

Clients: ``openai-agents`` (T01..T15, unchanged, see run_matrix.py), ``codex-cli`` (C01..C13,
needs PEXISLINK_CODEX_BIN), ``claude-code`` (K01..K08, needs PEXISLINK_CLAUDE_BIN, local only) and
``claude-agent-sdk`` (not run: every documented entry point starts a model turn).

Per matrix id: PASS when every test that ran passed and none was skipped as not run; FAIL when any
test failed or errored; NOT_RUN when the tests were skipped because the client, a credential or a
model would be needed; NOT_SUPPORTED when the client's public interface cannot express the check.
Skip reasons must start with ``not_run:`` or ``not_supported:``; any other skip counts as FAIL, so a
silently skipped test can never be reported as passing. ``--require`` exits 1 unless every id of the
named clients is PASS (ids that are NOT_SUPPORTED by design are listed in ``EXPECTED_NOT_SUPPORTED``).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATUSES = ("PASS", "FAIL", "NOT_RUN", "NOT_SUPPORTED")
CASES = ("initialize", "configured_vs_executed", "discover_five_tools", "authorized_query", "denied_access",
         "forged_identity", "expired_and_revoked", "session_isolation", "schemas_and_pagination",
         "error_sanitization", "determinism", "no_write_tools_no_effects", "process_shutdown")
CLIENTS = {
    "openai-agents": {
        "module": "compat.test_openai_agents_stdio", "prefix": "T",
        "interface": "agents.mcp.MCPServerStdio (Python SDK), no model calls",
        "cases": {"initialize": ["T01"], "configured_vs_executed": ["T03"], "discover_five_tools": ["T02"],
                  "authorized_query": ["T03"], "denied_access": ["T04"], "forged_identity": ["T05"],
                  "expired_and_revoked": ["T06"], "session_isolation": ["T11"], "schemas_and_pagination": ["T07"],
                  "error_sanitization": ["T10"], "determinism": ["T12"], "no_write_tools_no_effects": ["T13"],
                  "process_shutdown": ["T14"]},
    },
    "codex-cli": {
        "module": "compat.test_codex_app_server", "prefix": "C",
        "interface": "codex app-server JSON-RPC (mcpServerStatus/list, mcpServer/tool/call) and codex mcp; "
                     "no turn, no model calls",
        "cases": {"initialize": ["C01"], "configured_vs_executed": ["C02"], "discover_five_tools": ["C03"],
                  "authorized_query": ["C04"], "denied_access": ["C05"], "forged_identity": ["C06"],
                  "expired_and_revoked": ["C07"], "session_isolation": ["C08"], "schemas_and_pagination": ["C09"],
                  "error_sanitization": ["C10"], "determinism": ["C11"], "no_write_tools_no_effects": ["C12"],
                  "process_shutdown": ["C13"]},
    },
    "claude-code": {
        "module": "compat.test_claude_code_cli", "prefix": "K",
        "interface": "claude mcp add/get/list (configuration and health check only), no model calls",
        "cases": {"initialize": ["K02"], "configured_vs_executed": ["K01", "K02", "K03", "K04"],
                  "discover_five_tools": ["K07"], "authorized_query": ["K08"], "denied_access": ["K08"],
                  "forged_identity": ["K08"], "expired_and_revoked": ["K08"], "session_isolation": ["K08"],
                  "schemas_and_pagination": ["K08"], "error_sanitization": ["K08"], "determinism": ["K08"],
                  "no_write_tools_no_effects": ["K02", "K08"], "process_shutdown": ["K06"]},
    },
}
STATIC = {
    "claude-agent-sdk": {
        "interface": "claude_agent_sdk (query / ClaudeSDKClient)",
        "status": "NOT_RUN",
        "reason": "every documented entry point starts a model turn, which needs an Anthropic credential and "
                  "billable model calls; no authorized, reproducible, credential-free test is possible",
    },
}
# Independent expected-ID manifest: deleting or renaming a test cannot silently
# reduce the required conformance gate. This MUST NOT be inferred from discovery.
EXPECTED_TEST_IDS = {
    "openai-agents": frozenset(f"T{i:02d}" for i in range(1, 16)),
    "codex-cli": frozenset(f"C{i:02d}" for i in range(1, 14)),
    "claude-code": frozenset(f"K{i:02d}" for i in range(1, 9)),
}
EXPECTED_NOT_SUPPORTED = {"codex-cli": set(), "openai-agents": set(), "claude-code": set()}
LIMITATIONS = {
    "openai-agents": ["I1 strict schema conversion unsatisfiable for read_observations mode=latest",
                      "I2 null for optional arguments is rejected", "I3 server_initialize_result is None",
                      "I4 duplicate tool names across servers need include_server_in_tool_names",
                      "I5 errors reach the model as text", "I6 /dev/null opened read-only by the stdio transport"],
    "codex-cli": ["app-server is marked experimental by Codex",
                  "the negotiated MCP protocol version is not exposed (C01 sub-check NOT_SUPPORTED)",
                  "Codex is native code: client-side effects are checked through its log, not an audit hook",
                  "startup plugin/app sync features reach the network unless disabled; the suite disables them "
                  "and blocks egress with an unroutable proxy"],
    "claude-code": ["proprietary: never installed by this repository or CI; local runs only",
                    "health check only: tools are listed and called only inside a model turn (K07, K08 NOT_RUN)",
                    "I7 the client's environment is passed to stdio servers (K05)"],
}


def version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def cli_version(env_var: str) -> str | None:
    binary = os.environ.get(env_var)
    if not binary or not os.path.isfile(binary):
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60,
                             env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/nonexistent"),
                                  "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else None


def run_module(module: str, prefix: str) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    ids: list[str] = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            ids.append(item.id())

    walk(suite)
    result = unittest.TestResult()
    suite.run(result)
    failed = {t.id() for t, _ in result.failures + result.errors}
    skipped = {t.id(): reason for t, reason in result.skipped}
    per_id: dict[str, list[str]] = defaultdict(list)
    reasons: dict[str, set[str]] = defaultdict(set)
    for test_id in ids:
        match = re.search(rf"\.test_({prefix}\d\d)_", test_id)
        if not match:
            continue
        key = match.group(1)
        if test_id in failed:
            per_id[key].append("fail")
        elif test_id in skipped:
            reason = skipped[test_id]
            kind = "not_run" if reason.startswith("not_run:") else \
                "not_supported" if reason.startswith("not_supported:") else "fail"
            per_id[key].append(kind)
            reasons[key].add(reason)
        else:
            per_id[key].append("pass")
    matrix = {}
    for key in sorted(per_id):
        states = per_id[key]
        if "fail" in states:
            status = "FAIL"
        elif "not_run" in states:
            status = "NOT_RUN"
        elif "pass" in states:
            status = "PASS"
        else:
            status = "NOT_SUPPORTED"
        matrix[key] = {"status": status, "tests": len(states), "passed": states.count("pass"),
                       "not_run": states.count("not_run"), "not_supported": states.count("not_supported"),
                       "reasons": sorted(reasons[key])}
    return {"matrix": matrix, "failures": sorted(failed)}


def matrix_integrity_errors(name: str, spec: dict, matrix: dict) -> list[str]:
    """Enforce a fixed test-ID contract, independently from test discovery.

    A green subset is not a green suite: a missing or renamed test is a failed
    evidence gate, including for clients not present in the current environment.
    """
    expected = EXPECTED_TEST_IDS[name]
    discovered = set(matrix)
    mapped = {test_id for ids in spec["cases"].values() for test_id in ids}
    problems = []
    for label, ids in (("missing_test_ids", expected - discovered),
                       ("unexpected_test_ids", discovered - expected),
                       ("unrecognized_case_mappings", mapped - expected)):
        if ids:
            problems.append(f"{name}: {label}: {', '.join(sorted(ids))}")
    if set(spec["cases"]) != set(CASES):
        problems.append(f"{name}: case_categories_do_not_match_the_declared_matrix")
    if any(not ids for ids in spec["cases"].values()):
        problems.append(f"{name}: empty_case_category")
    return problems


def required_client_errors(name: str, spec: dict, matrix: dict) -> list[str]:
    """Fail if even one required ID is missing or did not fully pass."""
    failures = matrix_integrity_errors(name, spec, matrix)
    for test_id in sorted(EXPECTED_TEST_IDS[name]):
        status = matrix.get(test_id, {}).get("status", "MISSING")
        if status != "PASS" and not (status == "NOT_SUPPORTED" and
                                     test_id in EXPECTED_NOT_SUPPORTED[name]):
            failures.append(f"{name}: {test_id} must PASS (got {status})")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    parser.add_argument("--require", nargs="*", default=[], choices=sorted(CLIENTS))
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10).stdout.strip() or None
    except OSError:
        commit = None
    try:
        from mcp_types.version import LATEST_PROTOCOL_VERSION
    except ImportError:
        LATEST_PROTOCOL_VERSION = None
    client_versions = {"openai-agents": version("openai-agents"), "codex-cli": cli_version("PEXISLINK_CODEX_BIN"),
                       "claude-code": cli_version("PEXISLINK_CLAUDE_BIN"), "claude-agent-sdk": version("claude-agent-sdk")}
    clients = {}
    exit_code = 0
    for name, spec in CLIENTS.items():
        run = run_module(spec["module"], spec["prefix"])
        cases = {}
        for case in CASES:
            keys = spec["cases"][case]
            states = [run["matrix"].get(k, {"status": "NOT_RUN"})["status"] for k in keys]
            cases[case] = {"ids": keys, "status": "FAIL" if "FAIL" in states else
                           "PASS" if all(s == "PASS" for s in states) else
                           "NOT_RUN" if "NOT_RUN" in states else "NOT_SUPPORTED"}
        summary = {s: sum(1 for m in run["matrix"].values() if m["status"] == s) for s in STATUSES}
        integrity_errors = matrix_integrity_errors(name, spec, run["matrix"])
        required_errors = required_client_errors(name, spec, run["matrix"]) if name in args.require else []
        clients[name] = {"interface": spec["interface"], "version": client_versions[name],
                         "executed": summary["PASS"] + summary["FAIL"] > 0, "summary": summary,
                         "matrix": run["matrix"], "cases": cases, "failures": run["failures"],
                         "integrity_errors": integrity_errors, "required_errors": required_errors,
                         "limitations": LIMITATIONS.get(name, [])}
        if run["failures"] or integrity_errors or required_errors:
            exit_code = 1
    for name, spec in STATIC.items():
        clients[name] = {"interface": spec["interface"], "version": client_versions[name], "executed": False,
                         "summary": {s: 0 for s in STATUSES} | {spec["status"]: len(CASES)},
                         "cases": {case: {"ids": [], "status": spec["status"]} for case in CASES},
                         "reason": spec["reason"], "limitations": []}
    report = {
        "schema": "pexislink.compatibility.multi_client.v0",
        "server": "pexislink fabric mcp-stdio (+ tests/compat/_fixture_server.py)",
        "command": "PYTHONPATH=src python tests/compat/run_clients.py",
        "statuses": {"PASS": "every test that ran passed", "FAIL": "a test failed or errored",
                     "NOT_RUN": "needs a client, credential or model call that is not available",
                     "NOT_SUPPORTED": "the client's public interface cannot express the check"},
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": f"{platform.system()}-{platform.machine()}", "git_commit": commit,
                        "server_packages": {p: version(p) for p in ("mcp", "mcp-types")},
                        "server_latest_mcp_protocol": LATEST_PROTOCOL_VERSION,
                        "model_calls": 0, "credentials": "none"},
        "clients": clients,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
