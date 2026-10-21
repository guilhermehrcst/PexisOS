"""Mission orchestration: plan, gate, run in a disposable workspace, verify, record.

Responsibilities stay separate on purpose:

* the provider only proposes a plan;
* the operator (or an explicitly named harness) approves a specific plan hash;
* the gateway authorizes every step;
* the sandbox is the only place code runs, and only after its probe passed;
* verification is computed here, by the runtime, from measured state — never taken from a
  provider's claim.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .mission import (VERIFICATION_COMMANDS, MissionSpec, MissionState, PlanRejected, is_protected, is_writable,
                      sha256_json, validate_plan)
from .providers import ProviderError, build_provider
from .providers.base import PlanRequest
from .redact import redact_text
from .sandbox import DisabledSandbox, DockerSandbox, SandboxUnavailable
from .store import Store, StoreError, utc_now
from .tools import BUILTIN_TOOLS, Gateway, MissionContext, tool_schemas
from .workspace import Workspace, WorkspaceError, changed_paths, snapshot, tree_sha256, unified_diff

S = MissionState
_SNAPSHOT_FILE_LIMIT = 64 * 1024
_SNAPSHOT_TOTAL_LIMIT = 512 * 1024


class EngineError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def default_sandbox(spec: MissionSpec):
    sandbox = spec.raw["sandbox"]
    if sandbox["backend"] == "docker":
        return DockerSandbox(sandbox["image"])
    return DisabledSandbox()


class Engine:
    def __init__(self, home: Path | str, *, clock: Callable[[], str] = utc_now,
                 sandbox_factory: Callable[[MissionSpec], Any] = default_sandbox,
                 connector_factory: Callable[[MissionSpec], Any] | None = None,
                 provider_factory: Callable[..., Any] = build_provider):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.store = Store(self.home / "petron.sqlite3", clock)
        self.sandbox_factory = sandbox_factory
        self.connector_factory = connector_factory
        self.provider_factory = provider_factory
        self.gateway = Gateway()

    def close(self) -> None:
        self.store.close()

    # create / plan ------------------------------------------------------------------------
    def create(self, data: Any) -> str:
        spec = MissionSpec.parse(data)
        source = Path(spec.raw["workspace"]["source"])
        if not source.is_absolute() or not source.is_dir():
            raise EngineError("workspace_source_invalid")
        source_files = snapshot(source)
        self.store.create_mission(spec)
        self.store.append_event(spec.mission_id, "workspace.source",
                                {"tree_sha256": tree_sha256(source_files), "files": len(source_files)})
        return spec.mission_id

    def _source_tree(self, mission_id: str) -> str:
        for event in self.store.events(mission_id):
            if event["kind"] == "workspace.source":
                return event["payload"]["tree_sha256"]
        raise EngineError("source_hash_missing")

    def _plan_request(self, spec: MissionSpec) -> PlanRequest:
        source = Path(spec.raw["workspace"]["source"])
        files, total = {}, 0
        for rel in sorted(snapshot(source)):
            text = (source / rel).read_text(encoding="utf-8", errors="replace")
            if len(text) > _SNAPSHOT_FILE_LIMIT or total + len(text) > _SNAPSHOT_TOTAL_LIMIT:
                continue
            files[rel] = redact_text(text)
            total += len(text)
        tools = {name: {"description": tool.description, "effect": tool.effect, "input_schema": tool.input_schema}
                 for name, tool in BUILTIN_TOOLS.items() if name in spec.tools}
        return PlanRequest(spec.mission_id, spec.raw["objective"], tools, files, spec.writable, spec.protected,
                           spec.budget["max_steps"])

    def plan(self, mission_id: str, *, allow_real_model: bool = False) -> str:
        spec = self.store.spec(mission_id)
        self.store.transition(mission_id, S.CREATED, S.PLANNING)
        try:
            provider = self.provider_factory(spec.raw["provider"], allow_real_model=allow_real_model)
            if not provider.simulated:
                used = sum(1 for e in self.store.events(mission_id) if e["kind"] == "provider.call")
                if used >= spec.budget["max_model_calls"]:
                    raise ProviderError("model_call_budget_exhausted")
            self.store.append_event(mission_id, "provider.call",
                                    {"provider": provider.kind, "simulated": provider.simulated})
            proposal = provider.propose_plan(self._plan_request(spec))
            plan = validate_plan(proposal.plan, spec, tool_schemas())
        except ProviderError as error:
            self.store.transition(mission_id, S.PLANNING, S.FAILED, reason=f"provider_error:{error.code}")
            raise EngineError(f"provider_error:{error.code}") from None
        except PlanRejected as rejected:
            self.store.append_event(mission_id, "plan.rejected", {"reason": rejected.reason})
            self.store.transition(mission_id, S.PLANNING, S.FAILED, reason=f"plan_rejected:{rejected.reason}")
            raise EngineError(f"plan_rejected:{rejected.reason}") from None
        plan_sha256 = sha256_json(plan)
        self.store.set_plan(mission_id, plan, plan_sha256, proposal.info)
        self.store.transition(mission_id, S.PLANNING, S.AWAITING_APPROVAL, payload={"plan_sha256": plan_sha256})
        return plan_sha256

    def approve(self, mission_id: str, plan_sha256: str, approver: str, channel: str) -> None:
        if not approver.strip() or not channel.strip():
            raise EngineError("approver_required")
        try:
            self.store.record_approval(mission_id, plan_sha256, approver, channel)
        except StoreError as error:
            raise EngineError(error.code) from None

    def cancel(self, mission_id: str, reason: str = "operator_cancelled") -> None:
        state = self.store.state(mission_id)
        try:
            self.store.transition(mission_id, state, S.CANCELLED, reason=reason)
        except Exception as error:
            raise EngineError(getattr(error, "code", "illegal_transition")) from None

    # run / verify -------------------------------------------------------------------------
    def _fail(self, mission_id: str, current: MissionState, reason: str) -> None:
        try:
            self.store.transition(mission_id, current, S.FAILED, reason=reason)
        except StoreError:
            pass  # someone else (e.g. a cancel) moved the mission first; their state stands

    def run(self, mission_id: str) -> MissionState:
        row = self.store.mission(mission_id)
        spec = self.store.spec(mission_id)
        if row["state"] != S.AWAITING_APPROVAL.value:
            raise EngineError("not_awaiting_approval")
        plan_sha256 = row["plan_sha256"]
        if self.store.approval_for(mission_id, plan_sha256) is None:
            raise EngineError("approval_missing")
        try:
            self.store.transition(mission_id, S.AWAITING_APPROVAL, S.RUNNING, payload={"plan_sha256": plan_sha256})
        except StoreError as error:
            raise EngineError(error.code) from None
        current = S.RUNNING
        try:
            plan = json.loads(row["plan_json"])
            if sha256_json(plan) != plan_sha256:
                self._fail(mission_id, current, "plan_integrity")
                return self.store.state(mission_id)
            sandbox = self.sandbox_factory(spec)
            probe = sandbox.probe()
            self.store.append_event(mission_id, "sandbox.probe", probe.to_dict())
            if not probe.ok:
                self.store.append_event(mission_id, "execution.disabled", {"reason": probe.reason})
            area = self.home / "workspaces" / mission_id
            source = Path(spec.raw["workspace"]["source"])
            base = Workspace.create(source, area, "base")
            work = Workspace.create(source, area, "work")
            self.store.append_event(mission_id, "workspace.created",
                                    {"tree_sha256": tree_sha256(snapshot(work.root))})
            argv = VERIFICATION_COMMANDS[spec.raw["verification"]["command"]]
            if probe.ok:
                baseline = sandbox.run(base.root, argv, spec.budget["exec_timeout_s"])
                self.store.append_event(mission_id, "verification.baseline",
                                        {"exit_code": baseline.exit_code, "timed_out": baseline.timed_out})
                self.store.put_artifact(mission_id, "tests.before.txt", baseline.stdout + baseline.stderr)
            connector = self.connector_factory(spec) if (self.connector_factory and "pexisos" in spec.raw) else None
            ctx = MissionContext(self.store, spec, work, sandbox, plan_sha256, connector)
            for index, step in enumerate(plan["steps"]):
                outcome = self.gateway.call(ctx, step["tool"], step["args"])
                self.store.append_event(mission_id, "step.outcome",
                                        {"index": index, "tool": step["tool"], "ok": outcome["ok"],
                                         "error": outcome.get("error")})
                if not outcome["ok"]:
                    self._fail(mission_id, current, f"step_failed:{index}:{outcome['error']}")
                    return self.store.state(mission_id)
            self.store.transition(mission_id, S.RUNNING, S.VERIFYING)
            current = S.VERIFYING
            return self._verify(mission_id, spec, base, work, sandbox, probe.ok, argv)
        except Exception as error:
            self._fail(mission_id, current, f"internal_error:{type(error).__name__}")
            return self.store.state(mission_id)

    def _verify(self, mission_id: str, spec: MissionSpec, base: Workspace, work: Workspace, sandbox: Any,
                isolation_ok: bool, argv: tuple[str, ...]) -> MissionState:
        before, after = snapshot(base.root), snapshot(work.root)
        changed = changed_paths(before, after)
        protected = sorted(p for p in set(before) | set(after) if is_protected(spec, p))
        checks: dict[str, bool] = {
            "changes_within_writable_paths": all(is_writable(spec, p) for p in changed),
            "protected_paths_unchanged": all(before.get(p) == after.get(p) for p in protected),
            "source_tree_unchanged": tree_sha256(snapshot(Path(spec.raw["workspace"]["source"])))
                                     == self._source_tree(mission_id),
        }
        diff = unified_diff(base.root, work.root, changed)
        self.store.put_artifact(mission_id, "diff.patch", diff)
        tests: dict[str, Any] = {"ran": False}
        if isolation_ok:
            try:
                result = sandbox.run(work.root, argv, spec.budget["exec_timeout_s"])
                tests = {"ran": True, "exit_code": result.exit_code, "timed_out": result.timed_out}
                self.store.put_artifact(mission_id, "tests.after.txt", result.stdout + result.stderr)
                checks["tests_pass_in_sandbox"] = result.exit_code == 0 and not result.timed_out
            except SandboxUnavailable as error:
                tests["reason"] = error.reason
                checks["tests_pass_in_sandbox"] = False
        else:
            tests["reason"] = "isolation_unavailable"
            checks["tests_pass_in_sandbox"] = False  # fail closed: unverified is not verified
        chain_ok, _ = self.store.verify_chain()
        checks["event_chain_intact"] = chain_ok
        verdict = {"checks": checks, "changed_paths": changed, "protected_paths": protected, "tests": tests,
                   "diff_sha256": sha256_json(diff)}
        self.store.append_event(mission_id, "verification.result", verdict)
        failed = sorted(name for name, ok in checks.items() if not ok)
        if failed:
            self._fail(mission_id, S.VERIFYING, "verification_failed:" + ",".join(failed))
        else:
            self.store.transition(mission_id, S.VERIFYING, S.COMPLETED)
        return self.store.state(mission_id)

    def discard_workspaces(self, mission_id: str) -> None:
        state = self.store.state(mission_id)
        if state not in (S.COMPLETED, S.FAILED, S.CANCELLED):
            raise EngineError("mission_active")
        shutil.rmtree(self.home / "workspaces" / mission_id, ignore_errors=True)
        self.store.append_event(mission_id, "workspace.discarded", {})


__all__ = ["Engine", "EngineError", "WorkspaceError"]
