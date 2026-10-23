"""Mission report built only from persisted evidence (mission row, events, artifacts)."""

from __future__ import annotations

import json
import unicodedata

from .redact import redact
from .store import Store

SIMULATED_NOTICE = ("SIMULATED PROVIDER: the plan came from a deterministic script. This run demonstrates "
                    "PETRON's infrastructure (state machine, gates, sandbox, verification, evidence), "
                    "not autonomous problem solving.")
SIMULATED_AGENT_NOTICE = ("SIMULATED PROVIDER: decisions came from a deterministic policy that branches on real "
                          "observations but whose repair hypotheses were written in advance. This run demonstrates "
                          "the agent loop (decide, act, observe, adapt, verify), not autonomous reasoning.")


def terminal_safe(text: str) -> str:
    """Make untrusted text safe to print: control characters (ESC, BEL, CR, C1), bidi overrides and
    other invisible format characters become visible escapes. Newlines and tabs are kept. The raw
    text stays in the stored artifact; only its rendering is escaped."""
    out = []
    for char in str(text):
        if char in "\n\t":
            out.append(char)
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            code = ord(char)
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}" if code < 0x10000
                       else f"\\U{code:08x}")
        else:
            out.append(char)
    return "".join(out)


def _line(value) -> str:
    """One logical line of a report: no embedded newlines, no control characters."""
    return terminal_safe(str(value)).replace("\n", "\\n").replace("\t", " ")


def _cell(value, limit: int | None = None) -> str:
    """One Markdown table cell from untrusted text."""
    text = _line(value)
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    return text.replace("|", "\\|")


def build_report(store: Store, mission_id: str) -> dict:
    mission = store.mission(mission_id)
    events = store.events(mission_id)
    by_kind: dict[str, list[dict]] = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event["payload"])
    spec = json.loads(mission["spec_json"])
    mode = spec.get("mode", "plan")
    plan_event = (by_kind.get("plan.proposed") or [None])[-1]
    turns = by_kind.get("agent.turn", [])
    if mode == "agent":
        provider = turns[-1]["provider"] if turns else {"provider": spec["provider"]["kind"],
                                                        "simulated": spec["provider"]["kind"] == "deterministic"}
    else:
        provider = plan_event["provider"] if plan_event else None
    simulated = bool(provider and provider.get("simulated"))
    notice = (SIMULATED_AGENT_NOTICE if mode == "agent" else SIMULATED_NOTICE) if simulated else None
    chain_ok, bad_seq = store.verify_chain()
    diff = store.artifact(mission_id, "diff.patch")
    return redact({
        "schema": "petron.report.v0",
        "mission_id": mission_id,
        "mode": mode,
        "objective": spec["objective"],
        "spec_sha256": mission["spec_sha256"],
        "final_state": mission["state"],
        "failure_reason": mission["failure_reason"],
        "provider": provider,
        "simulated_provider": simulated,
        "notice": notice,
        "plan_sha256": mission["plan_sha256"],
        "plan_steps": plan_event["steps"] if plan_event else [],
        "approvals": by_kind.get("plan.approved", []),
        "states": [(p["from"], p["to"], p.get("reason")) for p in by_kind.get("mission.state", [])],
        "sandbox_probe": (by_kind.get("sandbox.probe") or [None])[-1],
        "execution_disabled": by_kind.get("execution.disabled", []),
        "baseline_tests": (by_kind.get("verification.baseline") or [None])[-1],
        "tool_decisions": by_kind.get("tool.authorization", []),
        "step_outcomes": by_kind.get("step.outcome", []),
        "agent_turns": [_turn_line(t) for t in turns],
        "agent_outcome": (by_kind.get("agent.outcome") or [None])[-1],
        "resumed": by_kind.get("agent.resumed", []),
        "usage": _usage(turns),
        "verification": (by_kind.get("verification.result") or [None])[-1],
        "diff_sha256": diff["sha256"] if diff else None,
        "diff": diff["content"] if diff else None,
        "event_count": len(events),
        "event_chain": {"ok": chain_ok, "first_bad_seq": bad_seq},
    })   # redacted again on the way out: rows written by older versions may hold plaintext


def _turn_line(turn: dict) -> dict:
    decision, observation = turn["decision"], turn["observation"]
    result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
    summary = result.get("summary") if isinstance(result, dict) else None
    return {"iteration": turn["iteration"], "kind": decision.get("kind"), "tool": decision.get("tool"),
            "ok": observation.get("ok"), "error_class": observation.get("error_class"),
            "error": observation.get("error"), "workspace_changed": observation.get("workspace_changed"),
            "failures": (summary or {}).get("failures") if summary else None,
            "rationale": (decision.get("rationale") or "")[:160]}


def _usage(turns: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
    for turn in turns:
        usage = (turn.get("provider") or {}).get("usage") or {}
        if not (turn.get("provider") or {}).get("simulated", True):
            totals["model_calls"] += 1
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
    return totals


def render_markdown(report: dict) -> str:
    lines = [f"# PETRON mission report — `{_line(report['mission_id'])}`", ""]
    if report["notice"]:
        lines += [f"> **{_line(report['notice'])}**", ""]
    lines += [
        f"- Objective: {_line(report['objective'])}",
        f"- Final state: **{_line(report['final_state'])}**" + (f" (`{_line(report['failure_reason'])}`)"
                                                               if report["failure_reason"] else ""),
        f"- Spec sha256: `{report['spec_sha256']}`",
        f"- {'Scope' if report['mode'] == 'agent' else 'Plan'} sha256 (approved): `{report['plan_sha256']}`",
        f"- Provider: `{_line(json.dumps(report['provider'], sort_keys=True))}`",
        f"- Event chain (unkeyed hash chain; see `petron events verify`): "
        f"{'intact' if report['event_chain']['ok'] else 'BROKEN at ' + str(report['event_chain']['first_bad_seq'])}"
        f" ({report['event_count']} mission events)",
        "", "## State transitions", "",
    ]
    lines += [f"- {_line(a)} → {_line(b)}" + (f" — `{_line(r)}`" if r else "") for a, b, r in report["states"]]
    lines += ["", "## Approvals", ""]
    lines += [f"- `{_line(a['plan_sha256'][:16])}…` by **{_line(a['approver'])}** via `{_line(a['channel'])}`"
              for a in report["approvals"]] \
        or ["- none"]
    probe = report["sandbox_probe"]
    lines += ["", "## Isolation", ""]
    if probe:
        lines.append(f"- Backend `{_line(probe['backend'])}`: {'verified' if probe['ok'] else 'NOT verified'}"
                     + (f" (`{_line(probe['reason'])}`)" if probe.get("reason") else ""))
        lines += [f"  - {'PASS' if ok else 'FAIL'} {_line(name)}" for name, ok in sorted(probe["checks"].items())]
    else:
        lines.append("- not probed (mission did not reach execution)")
    if report["mode"] == "agent":
        lines += ["", "## Agent turns", "", "| # | decision | tool | result | failures | rationale |",
                  "|---|---|---|---|---|---|"]
        for t in report["agent_turns"]:
            result = "ok" if t["ok"] else (t["error_class"] or "failed")
            if t["workspace_changed"]:
                result += " (workspace changed)"
            failures = "" if t["failures"] is None else str(t["failures"])
            # Model-written text: one cell, no newlines, no control characters, no pipe.
            lines.append(f"| {_cell(t['iteration'])} | {_cell(t['kind'])} | {_cell(t['tool'] or '')} | "
                         f"{_cell(result)} | {_cell(failures)} | {_cell(t['rationale'], 240)} |")
        outcome = report["agent_outcome"] or {}
        lines.append("")
        lines.append(f"- Loop outcome: "
                     f"{'handed to runtime verification' if outcome.get('verify') else _line(outcome.get('reason'))}")
        if report["resumed"]:
            lines.append(f"- Resumed from checkpoint(s): {_line([r['from_iteration'] for r in report['resumed']])}")
        if report["usage"]["model_calls"]:
            lines.append(f"- Model usage: {_line(report['usage'])}")
    else:
        lines += ["", "## Steps", ""]
        lines += [f"- [{_line(o['index'])}] `{_line(o['tool'])}`: {'ok' if o['ok'] else 'FAILED ' + _line(o['error'])}"
                  for o in report["step_outcomes"]] or ["- none executed"]
    baseline = report["baseline_tests"]
    lines += ["", "## Verification (runtime-owned)", ""]
    if baseline:
        lines.append(f"- Baseline tests before change: exit {_line(baseline['exit_code'])}"
                     + (" (defect reproduced)" if baseline["exit_code"] != 0 else " (no failing test before change)"))
    verification = report["verification"]
    if verification:
        lines += [f"- {'PASS' if ok else 'FAIL'} {_line(name)}" for name, ok in sorted(verification["checks"].items())]
        lines.append(f"- Changed paths: {_line(', '.join(verification['changed_paths']) or 'none')}")
    else:
        lines.append("- not reached")
    if report["diff"]:
        diff = terminal_safe(report["diff"]).rstrip("\n")
        longest = max((len(run) for run in __import__("re").findall(r"`+", diff)), default=0)
        fence = "`" * max(3, longest + 1)   # a diff line of backticks cannot close the block early
        lines += ["", "## Diff", "", f"{fence}diff", diff, fence]
    return "\n".join(lines) + "\n"
