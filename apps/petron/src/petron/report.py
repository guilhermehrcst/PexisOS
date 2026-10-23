"""Mission report built only from persisted evidence (mission row, events, artifacts)."""

from __future__ import annotations

import json

from .redact import redact
from .store import Store

SIMULATED_NOTICE = ("SIMULATED PROVIDER: the plan came from a deterministic script. This run demonstrates "
                    "PETRON's infrastructure (state machine, gates, sandbox, verification, evidence), "
                    "not autonomous problem solving.")


def build_report(store: Store, mission_id: str) -> dict:
    mission = store.mission(mission_id)
    events = store.events(mission_id)
    by_kind: dict[str, list[dict]] = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event["payload"])
    plan_event = (by_kind.get("plan.proposed") or [None])[-1]
    provider = plan_event["provider"] if plan_event else None
    simulated = bool(provider and provider.get("simulated"))
    chain_ok, bad_seq = store.verify_chain()
    diff = store.artifact(mission_id, "diff.patch")
    return redact({
        "schema": "petron.report.v0",
        "mission_id": mission_id,
        "objective": json.loads(mission["spec_json"])["objective"],
        "spec_sha256": mission["spec_sha256"],
        "final_state": mission["state"],
        "failure_reason": mission["failure_reason"],
        "provider": provider,
        "simulated_provider": simulated,
        "notice": SIMULATED_NOTICE if simulated else None,
        "plan_sha256": mission["plan_sha256"],
        "plan_steps": plan_event["steps"] if plan_event else [],
        "approvals": by_kind.get("plan.approved", []),
        "states": [(p["from"], p["to"], p.get("reason")) for p in by_kind.get("mission.state", [])],
        "sandbox_probe": (by_kind.get("sandbox.probe") or [None])[-1],
        "execution_disabled": by_kind.get("execution.disabled", []),
        "baseline_tests": (by_kind.get("verification.baseline") or [None])[-1],
        "tool_decisions": by_kind.get("tool.authorization", []),
        "step_outcomes": by_kind.get("step.outcome", []),
        "verification": (by_kind.get("verification.result") or [None])[-1],
        "diff_sha256": diff["sha256"] if diff else None,
        "diff": diff["content"] if diff else None,
        "event_count": len(events),
        "event_chain": {"ok": chain_ok, "first_bad_seq": bad_seq},
    })   # redacted again on the way out: rows written by older versions may hold plaintext


def render_markdown(report: dict) -> str:
    lines = [f"# PETRON mission report — `{report['mission_id']}`", ""]
    if report["notice"]:
        lines += [f"> **{report['notice']}**", ""]
    lines += [
        f"- Objective: {report['objective']}",
        f"- Final state: **{report['final_state']}**" + (f" (`{report['failure_reason']}`)"
                                                        if report["failure_reason"] else ""),
        f"- Spec sha256: `{report['spec_sha256']}`",
        f"- Plan sha256: `{report['plan_sha256']}`",
        f"- Provider: `{json.dumps(report['provider'], sort_keys=True)}`",
        f"- Event chain: {'intact' if report['event_chain']['ok'] else 'BROKEN at ' + str(report['event_chain']['first_bad_seq'])}"
        f" ({report['event_count']} mission events)",
        "", "## State transitions", "",
    ]
    lines += [f"- {a} → {b}" + (f" — `{r}`" if r else "") for a, b, r in report["states"]]
    lines += ["", "## Approvals", ""]
    lines += [f"- `{a['plan_sha256'][:16]}…` by **{a['approver']}** via `{a['channel']}`" for a in report["approvals"]] \
        or ["- none"]
    probe = report["sandbox_probe"]
    lines += ["", "## Isolation", ""]
    if probe:
        lines.append(f"- Backend `{probe['backend']}`: {'verified' if probe['ok'] else 'NOT verified'}"
                     + (f" (`{probe['reason']}`)" if probe.get("reason") else ""))
        lines += [f"  - {'PASS' if ok else 'FAIL'} {name}" for name, ok in sorted(probe["checks"].items())]
    else:
        lines.append("- not probed (mission did not reach execution)")
    lines += ["", "## Steps", ""]
    lines += [f"- [{o['index']}] `{o['tool']}`: {'ok' if o['ok'] else 'FAILED ' + str(o['error'])}"
              for o in report["step_outcomes"]] or ["- none executed"]
    baseline = report["baseline_tests"]
    lines += ["", "## Verification (runtime-owned)", ""]
    if baseline:
        lines.append(f"- Baseline tests before change: exit {baseline['exit_code']}"
                     + (" (defect reproduced)" if baseline["exit_code"] != 0 else " (no failing test before change)"))
    verification = report["verification"]
    if verification:
        lines += [f"- {'PASS' if ok else 'FAIL'} {name}" for name, ok in sorted(verification["checks"].items())]
        lines.append(f"- Changed paths: {', '.join(verification['changed_paths']) or 'none'}")
    else:
        lines.append("- not reached")
    if report["diff"]:
        lines += ["", "## Diff", "", "```diff", report["diff"].rstrip("\n"), "```"]
    return "\n".join(lines) + "\n"
