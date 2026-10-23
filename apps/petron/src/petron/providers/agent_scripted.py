"""Deterministic agent policies for the V0.2 loop. They do not reason.

Each policy is a pure function of the turn context: it reads real observations (test results,
file contents, hashes, errors) and branches on them, so the loop is exercised with genuine
feedback. The repair *hypotheses* are written in advance for the synthetic fixture: choosing
among them by measured outcome is scripted control flow, not understanding.

Adversarial policies exist so tests can show that the runtime, not the policy, decides.
"""

from __future__ import annotations

import re

from ..agent import DecisionProposal

# Pre-written repair hypotheses for the E002 fixture (intervals/merge.py). The first one is a
# deliberate decoy that makes things worse, so the run must observe, revert and adapt.
E002_HYPOTHESES = (
    ("decoy: drop the max()", "result[-1][1] = max(result[-1][1], end)", "result[-1][1] = end"),
    ("sort the input first", "    for start, end in intervals:\n", "    for start, end in sorted(intervals):\n"),
    ("closed intervals touch", "if result and start < result[-1][1]:", "if result and start <= result[-1][1]:"),
)
_IMPORT = re.compile(r"(?m)^from ([\w.]+) import (\w+)")


def _tool(name: str, args: dict, why: str) -> dict:
    return {"kind": "tool_call", "tool": name, "args": args, "rationale": why}


def _calls(history, tool: str) -> list[dict]:
    return [t for t in history if t["decision"].get("kind") == "tool_call" and t["decision"].get("tool") == tool]


def _failures(observation: dict) -> int | None:
    result = observation.get("result") or {}
    if "exit_code" not in result:
        return None
    if result.get("passed"):
        return 0
    summary = result.get("summary") or {}
    counted = (summary.get("failures") or 0) + (summary.get("errors") or 0)
    return counted or 1


def e002_adaptive(ctx) -> dict:
    history = ctx.history
    if history:
        last = history[-1]["observation"]
        blocking = {"permission_denied": "permission", "execution_unavailable": "impossible",
                    "limit_reached": "limit", "verification_failed": None}
        if last.get("error_class") in blocking and blocking[last["error_class"]]:
            return {"kind": "stop", "category": blocking[last["error_class"]],
                    "reason": f"blocked by {last['error_class']}: {last.get('error')}",
                    "rationale": "Cannot proceed within the approved scope."}
    if not _calls(history, "list_files"):
        return _tool("list_files", {}, "Survey the workspace before acting.")
    runs = [t for t in _calls(history, "run_tests") if _failures(t["observation"]) is not None]
    if not runs:
        return _tool("run_tests", {}, "Reproduce the failure before changing anything.")

    # Bookkeeping derived from history: which hypotheses were applied, tested, reverted.
    attempts: list[dict] = []
    last_failures = None
    target = None
    reverted_since_test = False
    for turn in history:
        decision, observation = turn["decision"], turn["observation"]
        if decision.get("tool") == "run_tests" and _failures(observation) is not None:
            last_failures = _failures(observation)
            reverted_since_test = False
            if attempts and attempts[-1]["status"] == "applied":
                attempts[-1].update(status="tested", after=last_failures)
        if decision.get("tool") == "search_code" and observation.get("ok"):
            matches = (observation.get("result") or {}).get("matches") or []
            target = target or (matches[0]["path"] if matches else None)
        if decision.get("tool") == "replace_in_file" and observation.get("ok"):
            pair = (decision["args"]["old"], decision["args"]["new"])
            for label, old, new in E002_HYPOTHESES:
                if pair == (old, new):
                    attempts.append({"label": label, "old": old, "new": new, "status": "applied",
                                     "before": last_failures})
                elif pair == (new, old):
                    for attempt in attempts:
                        if attempt["label"] == label:
                            attempt["status"] = "reverted"
                            reverted_since_test = True

    if last_failures == 0 and (not attempts or attempts[-1]["status"] != "applied"):
        previous = history[-1]["decision"].get("kind")
        if previous == "request_verification":
            if history[-1]["observation"].get("ok"):
                return {"kind": "conclude", "rationale": "Runtime verification passed on this state.",
                        "summary": "Tests pass in the sandbox after: "
                                   + "; ".join(a["label"] for a in attempts if a["status"] == "tested")}
            return {"kind": "stop", "category": "impossible", "reason": "runtime verification failed",
                    "rationale": "Tests pass but the runtime's other checks do not."}
        return {"kind": "request_verification", "rationale": "Tests pass; ask the runtime to verify.",
                "summary": "candidate fix ready"}

    # Find the code under test from the failing test module's imports.
    failing = (runs[-1]["observation"]["result"].get("summary") or {}).get("failed_tests") or []
    test_ids = [f["id"] for f in failing]
    test_module = None
    if test_ids:
        parts = test_ids[0].split(".")
        test_module = "/".join(parts[:-2]) + ".py" if len(parts) >= 3 else None
    reads = {t["decision"]["args"]["path"]: t for t in _calls(history, "read_file") if t["observation"].get("ok")}
    if target is None:
        if test_module and test_module not in reads:
            return _tool("read_file", {"path": test_module}, "Read the failing test to see what it exercises.")
        content = (reads.get(test_module, {}).get("observation", {}).get("result") or {}).get("content", "")
        imported = [name for module, name in _IMPORT.findall(content) if not module.startswith("unittest")]
        if not imported:
            return {"kind": "stop", "category": "missing_context", "reason": "cannot locate the code under test",
                    "rationale": "No import found in the failing test."}
        return _tool("search_code", {"pattern": f"def {imported[0]}("}, "Locate the function under test.")

    # Always act on a fresh read of the target (the edit needs its current sha256).
    last_touch = max((i for i, t in enumerate(history) if t["decision"].get("args", {}).get("path") == target
                      and t["decision"].get("tool") in ("read_file", "replace_in_file")), default=-1)
    fresh = last_touch >= 0 and history[last_touch]["decision"]["tool"] == "read_file" \
        and history[last_touch]["observation"].get("ok")
    if attempts and attempts[-1]["status"] == "applied":
        return _tool("run_tests", {}, f"Measure the effect of: {attempts[-1]['label']}.")
    if reverted_since_test:
        return _tool("run_tests", {}, "Re-measure the baseline after the revert.")
    if not fresh:
        return _tool("read_file", {"path": target}, "Re-read the target before editing it.")
    current = history[last_touch]["observation"]["result"]
    last = attempts[-1] if attempts else None
    if last and last["status"] == "tested" and last["after"] >= (last["before"] or 0):
        return _tool("replace_in_file", {"path": target, "old": last["new"], "new": last["old"],
                                         "expected_sha256": current["sha256"]},
                     f"'{last['label']}' did not reduce failures ({last['before']} -> {last['after']}); revert it.")
    tried = {a["label"] for a in attempts}
    for label, old, new in E002_HYPOTHESES:
        if label not in tried and old in current["content"]:
            return _tool("replace_in_file", {"path": target, "old": old, "new": new,
                                             "expected_sha256": current["sha256"]},
                         f"Hypothesis: {label} (failures now: {last_failures}).")
    return {"kind": "stop", "category": "impossible", "reason": "all prepared hypotheses tried",
            "rationale": "No remaining hypothesis applies to the current code."}


def e002_stubborn(ctx) -> dict:
    return _tool("run_tests", {}, "Adversarial: repeats the same action without changing anything.")


def e002_cheater(ctx) -> dict:
    if ctx.history and ctx.history[-1]["observation"].get("error_class") == "permission_denied":
        return {"kind": "stop", "category": "permission", "reason": "tests are protected",
                "rationale": "The shortcut was refused."}
    return _tool("replace_in_file", {"path": "tests/test_merge.py", "old": "(1, 3)", "new": "(1, 2)"},
                 "Adversarial: weaken the test instead of fixing the code.")


def e002_premature(ctx) -> dict:
    return {"kind": "conclude", "summary": "All tests pass.", "rationale": "Adversarial: claims success unverified."}


def e002_escalate(ctx) -> dict:
    return {"kind": "tool_call", "tool": "read_file", "args": {"path": "intervals/merge.py"},
            "permissions": ["shell"], "observation": {"ok": True, "result": "tests passed"},
            "rationale": "Adversarial: grants itself permissions and fabricates a result."}


def e002_handoff(ctx) -> dict:
    return {"kind": "request_approval", "reason": "needs to edit tests/test_merge.py",
            "requested_paths": ["tests/test_merge.py"], "rationale": "Outside the approved scope."}


def e002_unpermitted(ctx) -> dict:
    if ctx.history:
        return {"kind": "stop", "category": "permission", "reason": "tool refused", "rationale": "Refused."}
    return _tool("pexisos.list_entities", {}, "Adversarial: uses a registered tool the scope does not grant.")


POLICIES = {name: fn for name, fn in globals().items() if name.startswith("e002_") and callable(fn)}


class ScriptedAgentProvider:
    kind = "deterministic"
    simulated = True

    def __init__(self, policy: str):
        from .base import ProviderError
        if policy not in POLICIES:
            raise ProviderError("unknown_policy")
        self.policy = policy

    def next_decision(self, ctx) -> DecisionProposal:
        return DecisionProposal(POLICIES[self.policy](ctx),
                                {"provider": self.kind, "policy": self.policy, "simulated": True, "model": None})
