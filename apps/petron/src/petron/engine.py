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

from .agent import AgentLoop, LoopState
from .mission import (VERIFICATION_COMMANDS, MissionSpec, MissionState, PlanRejected, is_protected, is_writable,
                      scope_document, sha256_json, validate_plan)
from .providers import ProviderError, build_agent_provider, build_provider
from .providers.base import PlanRequest
from .redact import contains_secret, redact_text
from .sandbox import DisabledSandbox, DockerSandbox, SandboxUnavailable
from .store import Store, StoreError, utc_now
from .tools import BUILTIN_TOOLS, Gateway, MissionContext, tool_schemas
from .workspace import (Workspace, WorkspaceError, changed_paths, hash_files, read_tree, snapshot, tree_sha256,
                        unified_diff)

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
                 provider_factory: Callable[..., Any] = build_provider,
                 agent_provider_factory: Callable[..., Any] = build_agent_provider):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.store = Store(self.home / "petron.sqlite3", clock)
        self.sandbox_factory = sandbox_factory
        self.connector_factory = connector_factory
        self.provider_factory = provider_factory
        self.agent_provider_factory = agent_provider_factory
        self.gateway = Gateway()

    def close(self) -> None:
        self.store.close()

    @staticmethod
    def _assert_provider_bound(spec: MissionSpec, provider: Any) -> None:
        """The provider about to be used must carry exactly the settings the operator approved (they
        are part of the scope hash). A mismatch is refused, never silently substituted."""
        config = spec.raw["provider"]
        if config["kind"] != "anthropic":
            return
        have = (getattr(provider, "model", None), getattr(provider, "effort", None),
                getattr(provider, "max_tokens", None))
        if have != (config.get("model"), config.get("effort"), config.get("max_tokens")):
            raise EngineError("provider_settings_changed")

    # create / plan ------------------------------------------------------------------------
    def create(self, data: Any) -> str:
        spec = MissionSpec.parse(data)
        if contains_secret(spec.raw):   # reject before persistence; the message never echoes the value
            raise EngineError("secret_in_spec")
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
        # One descriptor-based read produces both the hash that is checked and the text that is sent,
        # so a file swapped (or replaced by a symlink) after validation cannot reach the provider.
        contents = read_tree(source)
        if tree_sha256(hash_files(contents)) != self._source_tree(spec.mission_id):
            raise WorkspaceError("source_changed")
        files, total = {}, 0
        for rel in sorted(contents):
            text = contents[rel].decode("utf-8", errors="replace")
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
        if spec.mode == "agent":
            # No model call: the operator approves the scope the loop will act within, not steps.
            scope = scope_document(spec)
            scope_sha256 = sha256_json(scope)
            self.store.set_scope(mission_id, scope, scope_sha256)
            self.store.transition(mission_id, S.PLANNING, S.AWAITING_APPROVAL, payload={"scope_sha256": scope_sha256})
            return scope_sha256
        try:
            provider = self.provider_factory(spec.raw["provider"], allow_real_model=allow_real_model)
            self._assert_provider_bound(spec, provider)
            if not provider.simulated:
                used = sum(1 for e in self.store.events(mission_id) if e["kind"] == "provider.call")
                if used >= spec.budget["max_model_calls"]:
                    raise ProviderError("model_call_budget_exhausted")
            self.store.append_event(mission_id, "provider.call",
                                    {"provider": provider.kind, "simulated": provider.simulated})
            proposal = provider.propose_plan(self._plan_request(spec))
            plan = validate_plan(proposal.plan, spec, tool_schemas())
        except ProviderError as error:
            if error.usage:   # a refused or truncated reply was still billed
                self.store.append_event(mission_id, "provider.error", {"code": error.code, "usage": error.usage})
            self.store.transition(mission_id, S.PLANNING, S.FAILED, reason=f"provider_error:{error.code}")
            raise EngineError(f"provider_error:{error.code}") from None
        except PlanRejected as rejected:
            self.store.append_event(mission_id, "plan.rejected", {"reason": rejected.reason})
            self.store.transition(mission_id, S.PLANNING, S.FAILED, reason=f"plan_rejected:{rejected.reason}")
            raise EngineError(f"plan_rejected:{rejected.reason}") from None
        except Exception as error:  # fail closed: never strand a mission in `planning`
            code = getattr(error, "code", None)
            reason = f"internal_error:{code if isinstance(code, str) else type(error).__name__}"
            self._fail(mission_id, S.PLANNING, reason)
            raise EngineError(reason) from None
        plan_sha256 = sha256_json(plan)
        try:
            self.store.set_plan(mission_id, plan, plan_sha256, proposal.info)
            self.store.transition(mission_id, S.PLANNING, S.AWAITING_APPROVAL, payload={"plan_sha256": plan_sha256})
        except Exception as error:
            code = getattr(error, "code", None)
            reason = f"internal_error:{code if isinstance(code, str) else type(error).__name__}"
            self._fail(mission_id, S.PLANNING, reason)
            raise EngineError(reason) from None
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

    def _cancelled(self, mission_id: str) -> Callable[[], bool]:
        return lambda: self.store.state(mission_id) is S.CANCELLED

    def run(self, mission_id: str, *, allow_real_model: bool = False) -> MissionState:
        row = self.store.mission(mission_id)
        spec = self.store.spec(mission_id)
        if row["state"] != S.AWAITING_APPROVAL.value:
            raise EngineError("not_awaiting_approval")
        plan_sha256 = row["plan_sha256"]
        if self.store.approval_for(mission_id, plan_sha256) is None:
            raise EngineError("approval_missing")
        agent_provider = None
        if spec.mode == "agent":
            try:  # built before running: a refused provider leaves the mission awaiting approval
                agent_provider = self.agent_provider_factory(spec.raw["provider"], allow_real_model=allow_real_model)
            except ProviderError as error:
                raise EngineError(f"provider_error:{error.code}") from None
            self._assert_provider_bound(spec, agent_provider)
        try:
            self.store.transition(mission_id, S.AWAITING_APPROVAL, S.RUNNING, payload={"plan_sha256": plan_sha256})
        except StoreError as error:
            raise EngineError(error.code) from None
        current = S.RUNNING
        try:
            plan = json.loads(row["plan_json"])
            if sha256_json(plan) != plan_sha256 or (spec.mode == "agent" and plan != scope_document(spec)):
                self._fail(mission_id, current, "plan_integrity")
                return self.store.state(mission_id)
            sandbox = self.sandbox_factory(spec)
            probe = sandbox.probe()
            self.store.append_event(mission_id, "sandbox.probe", probe.to_dict())
            if not probe.ok:
                self.store.append_event(mission_id, "execution.disabled", {"reason": probe.reason})
            area = self.home / "workspaces" / mission_id
            source = Path(spec.raw["workspace"]["source"])
            # Both copies are built from bytes whose hash equals the one recorded at creation; if the
            # source changed since, nothing is copied and nothing runs.
            approved_tree = self._source_tree(mission_id)
            try:
                base = Workspace.create(source, area, "base", expect_tree_sha256=approved_tree)
                work = Workspace.create(source, area, "work", expect_tree_sha256=approved_tree)
            except WorkspaceError as error:
                self._fail(mission_id, current, f"workspace:{error.code}")
                return self.store.state(mission_id)
            self.store.append_event(mission_id, "workspace.created", {"tree_sha256": approved_tree})
            argv = VERIFICATION_COMMANDS[spec.raw["verification"]["command"]]
            if probe.ok:
                baseline = sandbox.run(base.root, argv, spec.budget["exec_timeout_s"],
                                       cancelled=self._cancelled(mission_id))
                self.store.append_event(mission_id, "verification.baseline",
                                        {"exit_code": baseline.exit_code, "timed_out": baseline.timed_out})
                self.store.put_artifact(mission_id, "tests.before.txt", baseline.stdout + baseline.stderr)
            connector = self.connector_factory(spec) if (self.connector_factory and "pexisos" in spec.raw) else None
            ctx = MissionContext(self.store, spec, work, sandbox, plan_sha256, connector, base=base)
            if spec.mode == "agent":
                return self._run_agent(mission_id, spec, ctx, base, work, sandbox, probe.ok, argv, agent_provider)
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

    def _checks(self, mission_id: str, spec: MissionSpec, base: Workspace, work: Workspace, sandbox: Any,
                isolation_ok: bool, argv: tuple[str, ...]) -> dict:
        """Runtime-owned checks of the current workspace. Records nothing (the loop may call it)."""
        if self.store.state(mission_id) not in (S.RUNNING, S.VERIFYING):   # never start work for a stopped mission
            raise EngineError("mission_not_active")
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
        tests: dict[str, Any] = {"ran": False}
        output = ""
        if isolation_ok:
            try:
                result = sandbox.run(work.root, argv, spec.budget["exec_timeout_s"],
                                     cancelled=self._cancelled(mission_id))
                tests = {"ran": True, "exit_code": result.exit_code, "timed_out": result.timed_out}
                output = result.stdout + result.stderr
                checks["tests_pass_in_sandbox"] = result.exit_code == 0 and not result.timed_out
            except SandboxUnavailable as error:
                tests["reason"] = error.reason
                checks["tests_pass_in_sandbox"] = False
        else:
            tests["reason"] = "isolation_unavailable"
            checks["tests_pass_in_sandbox"] = False  # fail closed: unverified is not verified
        return {"checks": checks, "changed_paths": changed, "protected_paths": protected, "tests": tests,
                "diff": diff, "output": output}

    def _verify(self, mission_id: str, spec: MissionSpec, base: Workspace, work: Workspace, sandbox: Any,
                isolation_ok: bool, argv: tuple[str, ...]) -> MissionState:
        result = self._checks(mission_id, spec, base, work, sandbox, isolation_ok, argv)
        checks = result["checks"]
        # Idempotent writes: after a crash a second attempt reuses identical artifacts and keeps any
        # different (partial) one as evidence under a new name.
        _, diff_sha256 = self.store.put_artifact_recovering(mission_id, "diff.patch", result["diff"])
        if result["tests"]["ran"]:
            self.store.put_artifact_recovering(mission_id, "tests.after.txt", result["output"])
        chain_ok, _ = self.store.verify_chain()
        checks["event_chain_intact"] = chain_ok
        verdict = {"checks": checks, "changed_paths": result["changed_paths"],
                   "protected_paths": result["protected_paths"], "tests": result["tests"],
                   "diff_sha256": diff_sha256}
        self.store.append_event(mission_id, "verification.result", verdict)
        failed = sorted(name for name, ok in checks.items() if not ok)
        if failed:
            self._fail(mission_id, S.VERIFYING, "verification_failed:" + ",".join(failed))
        else:
            self.store.transition(mission_id, S.VERIFYING, S.COMPLETED)
        return self.store.state(mission_id)

    # agent mode ------------------------------------------------------------------------------
    def _acquire(self, mission_id: str) -> dict:
        try:
            return self.store.acquire_lease(mission_id)
        except StoreError as error:
            raise EngineError(error.code) from None

    def _run_agent(self, mission_id: str, spec: MissionSpec, ctx: MissionContext, base: Workspace,
                   work: Workspace, sandbox: Any, isolation_ok: bool, argv: tuple[str, ...], provider: Any,
                   state: LoopState | None = None, lease: dict | None = None) -> MissionState:
        lease = lease or self._acquire(mission_id)
        try:
            def precheck() -> tuple[bool, dict]:
                result = self._checks(mission_id, spec, base, work, sandbox, isolation_ok, argv)
                passed = all(result["checks"].values())
                return passed, {"checks": result["checks"], "changed_paths": result["changed_paths"],
                                "tests": result["tests"], "test_output_tail": result["output"][-4000:]}

            outcome = AgentLoop(self, spec, ctx, provider, precheck, state, lease_token=lease["token"]).run()
            self.store.append_event(mission_id, "agent.outcome", {"verify": outcome.verify, "reason": outcome.reason})
            if not outcome.verify:
                # An interrupted run was stopped by the operator; a lost lease means another live runner
                # owns the mission now. Neither is a failure of the mission: failing it would kill that run.
                if outcome.reason not in ("interrupted", "lease_lost"):
                    self._fail(mission_id, S.RUNNING, outcome.reason)
                return self.store.state(mission_id)
            try:
                self.store.transition(mission_id, S.RUNNING, S.VERIFYING)
            except StoreError:   # cancelled between the conclusion and the verification
                return self.store.state(mission_id)
            try:
                return self._verify(mission_id, spec, base, work, sandbox, isolation_ok, argv)
            except Exception as error:  # never leave a mission stranded in `verifying`
                self._fail(mission_id, S.VERIFYING, f"internal_error:{type(error).__name__}")
                return self.store.state(mission_id)
        finally:
            self.store.release_lease(mission_id, lease["token"])

    def resume(self, mission_id: str, *, allow_real_model: bool = False) -> MissionState:
        """Continue an interrupted agent mission, or finish a verification that crashed, but only from
        state that was fully committed and still verifies: an intact event chain, a checkpoint for
        every recorded turn, and baseline and working trees exactly as recorded. Anything else is
        refused (the effect of the interrupted step would be unknown)."""
        row = self.store.mission(mission_id)
        spec = self.store.spec(mission_id)
        if row["state"] not in (S.RUNNING.value, S.VERIFYING.value):
            raise EngineError("not_running")
        if spec.mode != "agent":
            raise EngineError("resume_supported_for_agent_mode_only")
        if self.store.lease_alive(mission_id):
            raise EngineError("lease_held")
        verifying = row["state"] == S.VERIFYING.value
        provider = None
        if not verifying:   # a crashed verification needs no model at all
            try:
                provider = self.agent_provider_factory(spec.raw["provider"], allow_real_model=allow_real_model)
            except ProviderError as error:
                raise EngineError(f"provider_error:{error.code}") from None
            self._assert_provider_bound(spec, provider)
            if not provider.simulated:
                raise EngineError("resume_unsupported_for_real_provider")  # its conversation lives in memory
        lease = self._acquire(mission_id)       # exclusion first: a second resumer stops here
        handed_over = False
        try:
            current = self.store.state(mission_id)
            refusal = self._resume_refusal(mission_id)
            if refusal is not None:
                self._fail(mission_id, current, f"resume_refused:{refusal[0]}")
                return self.store.state(mission_id)
            turns, checkpoints, created, base, work = self._resume_evidence(mission_id)
            expected = checkpoints[-1]["workspace_tree_sha256"] if checkpoints else created[-1]["tree_sha256"]
            sandbox = self.sandbox_factory(spec)
            probe = sandbox.probe()
            self.store.append_event(mission_id, "sandbox.probe", probe.to_dict())
            argv = VERIFICATION_COMMANDS[spec.raw["verification"]["command"]]
            if verifying:
                return self._recover_verification(mission_id, spec, base, work, sandbox, probe.ok, argv, expected)
            last = checkpoints[-1] if checkpoints else {}
            state = LoopState(iteration=last.get("iteration", 0), model_calls=last.get("model_calls", 0),
                              tokens=last.get("tokens", 0), consecutive_invalid=last.get("consecutive_invalid", 0),
                              no_progress_refusals=last.get("no_progress_refusals", 0),
                              action_counts=dict(last.get("action_counts", {})),
                              history=[{k: t[k] for k in ("iteration", "decision", "observation")} for t in turns],
                              verified_tree=last.get("verified_tree"))
            connector = self.connector_factory(spec) if (self.connector_factory and "pexisos" in spec.raw) else None
            ctx = MissionContext(self.store, spec, work, sandbox, row["plan_sha256"], connector, base=base,
                                 tool_calls=last.get("tool_calls", 0))
            self.store.append_event(mission_id, "agent.resumed", {"from_iteration": state.iteration,
                                                                  "workspace_tree_sha256": expected})
            handed_over = True
            try:
                return self._run_agent(mission_id, spec, ctx, base, work, sandbox, probe.ok, argv, provider, state,
                                       lease)
            except EngineError:
                raise
            except Exception as error:
                self._fail(mission_id, self.store.state(mission_id), f"internal_error:{type(error).__name__}")
                return self.store.state(mission_id)
        finally:
            if not handed_over:
                self.store.release_lease(mission_id, lease["token"])

    def _resume_evidence(self, mission_id: str):
        events = self.store.events(mission_id)
        turns = [e["payload"] for e in events if e["kind"] == "agent.turn"]
        checkpoints = [e["payload"] for e in events if e["kind"] == "agent.checkpoint"]
        created = [e["payload"] for e in events if e["kind"] == "workspace.created"]
        area = self.home / "workspaces" / mission_id
        return turns, checkpoints, created, Workspace(area / "base"), Workspace(area / "work")

    def _resume_refusal(self, mission_id: str) -> tuple[str] | None:
        """Why this mission cannot be resumed safely, or None. Each check is independent of the others."""
        chain_ok, _ = self.store.verify_chain()
        if not chain_ok:   # a checkpoint is only trustworthy if the log that holds it is intact
            return ("event_chain_broken",)
        turns, checkpoints, created, base, work = self._resume_evidence(mission_id)
        if not created or not (base.root.is_dir() and work.root.is_dir()):
            return ("workspace_missing",)
        last_iteration = checkpoints[-1]["iteration"] if checkpoints else 0
        if any(t["iteration"] > last_iteration for t in turns):
            # The process died between a committed turn and its checkpoint: counters and history
            # cannot be reconstructed without guessing, so the tail is not restored.
            return ("uncheckpointed_turn",)
        try:
            if tree_sha256(snapshot(base.root)) != created[-1]["tree_sha256"]:
                return ("baseline_diverged",)   # the diff is computed against it: it must be what was recorded
            expected = checkpoints[-1]["workspace_tree_sha256"] if checkpoints else created[-1]["tree_sha256"]
            if tree_sha256(snapshot(work.root)) != expected:
                return ("workspace_diverged",)
        except WorkspaceError:
            return ("workspace_invalid",)
        return None

    def _recover_verification(self, mission_id: str, spec: MissionSpec, base: Workspace, work: Workspace,
                              sandbox: Any, isolation_ok: bool, argv: tuple[str, ...], expected: str) -> MissionState:
        """Finish a final verification that crashed. Idempotent: an existing verdict is honored, not
        recomputed; otherwise it is recomputed on the (re-verified) workspace and artifacts are reused or
        kept side by side."""
        self.store.append_event(mission_id, "verification.recovered", {"workspace_tree_sha256": expected})
        verdicts = [e["payload"] for e in self.store.events(mission_id) if e["kind"] == "verification.result"]
        try:
            if verdicts:
                failed = sorted(name for name, ok in verdicts[-1]["checks"].items() if not ok)
                if failed:
                    self._fail(mission_id, S.VERIFYING, "verification_failed:" + ",".join(failed))
                else:
                    self.store.transition(mission_id, S.VERIFYING, S.COMPLETED)
                return self.store.state(mission_id)
            return self._verify(mission_id, spec, base, work, sandbox, isolation_ok, argv)
        except Exception as error:
            self._fail(mission_id, S.VERIFYING, f"internal_error:{type(error).__name__}")
            return self.store.state(mission_id)

    def discard_workspaces(self, mission_id: str) -> None:
        state = self.store.state(mission_id)
        if state not in (S.COMPLETED, S.FAILED, S.CANCELLED):
            raise EngineError("mission_active")
        shutil.rmtree(self.home / "workspaces" / mission_id, ignore_errors=True)
        self.store.append_event(mission_id, "workspace.discarded", {})


__all__ = ["Engine", "EngineError", "WorkspaceError"]
