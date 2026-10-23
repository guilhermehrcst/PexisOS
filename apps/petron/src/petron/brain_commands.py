"""Mission entry from the Brain (opt-in): draft from words, approve by a deliberate gesture.

Enabled only by ``petron brain serve --interactive --template SPEC``. Authority stays where it was:

* the **operator** chooses the template at the terminal: workspace, tools, writable paths, limits,
  provider and sandbox. The browser can contribute only the objective text;
* a draft is an ordinary agent mission that stops at ``awaiting_approval`` (scope hash recorded);
* approval is a separate, explicit act bound to that scope hash; through the Brain it is allowed
  only for drafts *this bridge* created (each bridge run has its own identity, recorded with the
  draft); CLI missions, and drafts of another bridge or of an earlier run, keep CLI approval;
* execution uses the same engine, gateway, sandbox and verification as the CLI;
* a real (paid) provider in the template is refused unless the operator also passed
  ``--allow-real-model``.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import re
import secrets
import socket
import threading
from pathlib import Path

from .engine import Engine, EngineError
from .mission import MissionSpec, MissionState
from .schema import SchemaError
from .store import StoreError

OBJECTIVE_MAX = 500
_TERMINAL = {MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELLED}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MISSION_ID = re.compile(r"^brain-[0-9]{14}-[0-9a-f]{6}$")


class CommandRefused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class BrainCommands:
    def __init__(self, home: Path, template: dict, *, allow_real_model: bool = False, engine_factory=Engine):
        self.home = Path(home)
        self.allow_real_model = allow_real_model
        self.engine_factory = engine_factory
        self._lock = threading.Lock()
        self._runs: dict[str, threading.Thread] = {}
        probe = copy.deepcopy(template)
        probe["mission_id"] = "brain-00000000000000-000000"
        spec = MissionSpec.parse(probe)          # the template must already be a valid, closed spec
        if spec.mode != "agent":
            raise ValueError("Brain templates must be agent-mode missions (scope approval)")
        if spec.raw["provider"]["kind"] != "deterministic" and not allow_real_model:
            raise ValueError("template uses a real model provider; pass --allow-real-model to permit it")
        self.template = json.loads(json.dumps(template))
        self.bridge_id = secrets.token_hex(8)       # who may approve what: bound to each draft it creates
        self.template_sha256 = hashlib.sha256(json.dumps(self.template, sort_keys=True).encode()).hexdigest()

    def _engine(self) -> Engine:
        return self.engine_factory(self.home)  # one engine (SQLite connection) per call/thread

    def _is_brain_draft(self, engine: Engine, mission_id: str) -> bool:
        """True only for a draft created by this bridge run (the recorded identity must match)."""
        return any(e["kind"] == "brain.draft" and e["payload"].get("bridge") == self.bridge_id
                   for e in engine.store.events(mission_id))

    def _active_draft(self, engine: Engine) -> str | None:
        for row in engine.store.missions():
            if row["mission_id"].startswith("brain-") and MissionState(row["state"]) not in _TERMINAL \
                    and self._is_brain_draft(engine, row["mission_id"]):
                return row["mission_id"]
        return None

    def _abandon(self, engine: Engine, mission_id: str, error: Exception) -> CommandRefused:
        """Setup failed after (or while) the mission was persisted: end it, say so, and never pretend.

        Nothing persisted -> the request was rejected. Persisted -> cancelled with an auditable reason, so
        no active orphan blocks the next draft. If the cancellation itself cannot be persisted (the store is
        unavailable) the refusal says exactly that and the mission is left as it is for the operator."""
        try:
            engine.store.state(mission_id)
        except StoreError as missing:
            if missing.code == "unknown_mission":
                if isinstance(error, (SchemaError, EngineError, StoreError)):
                    return CommandRefused(400, "draft_rejected")
                return CommandRefused(500, "draft_failed")
            return CommandRefused(503, "draft_failed_not_cancelled")
        except Exception:
            return CommandRefused(503, "draft_failed_not_cancelled")
        try:
            engine.cancel(mission_id, reason="draft_setup_failed")
        except Exception:
            return CommandRefused(503, "draft_failed_not_cancelled")
        try:   # the cancellation (with its reason) is already persisted; this adds the cause, class name only
            engine.store.append_event(mission_id, "brain.draft_failed",
                                      {"cancelled": True, "error": type(error).__name__})
        except Exception:
            pass
        return CommandRefused(500, "draft_failed")

    def create_draft(self, objective: object) -> dict:
        if not isinstance(objective, str):
            raise CommandRefused(400, "objective_required")
        objective = _CONTROL.sub("", objective).strip()
        if not objective or len(objective) > OBJECTIVE_MAX:
            raise CommandRefused(400, "objective_length")
        with self._lock:
            engine = self._engine()
            try:
                if self._active_draft(engine):
                    raise CommandRefused(409, "draft_active")
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
                spec = copy.deepcopy(self.template)
                spec["mission_id"] = f"brain-{stamp}-{secrets.token_hex(3)}"
                spec["objective"] = objective
                mission_id = spec["mission_id"]
                try:
                    engine.create(spec)
                    engine.store.append_event(mission_id, "brain.draft", {
                        "channel": "brain", "bridge": self.bridge_id, "template_sha256": self.template_sha256,
                        "objective_sha256": hashlib.sha256(objective.encode()).hexdigest()})
                    scope_sha256 = engine.plan(mission_id)
                except Exception as error:
                    raise self._abandon(engine, mission_id, error) from None
                return {"mission_id": mission_id, "scope_sha256": scope_sha256, "state": "awaiting_approval"}
            finally:
                engine.close()

    def approve_and_run(self, mission_id: str, scope_sha256: object) -> dict:
        if not MISSION_ID.fullmatch(mission_id or "") or not isinstance(scope_sha256, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", scope_sha256):
            raise CommandRefused(400, "invalid_request")
        with self._lock:
            engine = self._engine()
            try:
                try:
                    state = engine.store.state(mission_id)
                except StoreError:
                    raise CommandRefused(404, "unknown_mission") from None
                if not self._is_brain_draft(engine, mission_id):
                    raise CommandRefused(403, "not_a_brain_draft")
                if state is not MissionState.AWAITING_APPROVAL:
                    raise CommandRefused(409, "not_awaiting_approval")
                try:
                    engine.approve(mission_id, scope_sha256, f"brain-operator@{socket.gethostname()}",
                                   "brain-hold-gesture")
                except EngineError as error:
                    raise CommandRefused(409, error.code) from None
            finally:
                engine.close()
            thread = threading.Thread(target=self._run, args=(mission_id,), name=f"petron-run-{mission_id}",
                                      daemon=True)
            self._runs[mission_id] = thread
            thread.start()
        # Approval is recorded; whether the runtime accepts the run is only known once it starts. The Brain
        # learns the outcome from the mission's own events, so this is not reported as "running".
        return {"mission_id": mission_id, "approved": scope_sha256, "state": "approved"}

    def _run(self, mission_id: str) -> None:
        engine = self._engine()
        try:
            engine.run(mission_id, allow_real_model=self.allow_real_model)
        except Exception as error:   # the runtime refused or could not start: end the mission, truthfully
            self._run_refused(engine, mission_id, error)
        finally:
            engine.close()

    def _run_refused(self, engine: Engine, mission_id: str, error: Exception) -> None:
        """The run never started although it was approved (provider setup refused, unexpected error).

        An approved scope that cannot run is ended as a terminal failure with an auditable event; the
        approval is neither rolled back nor reusable, and the Brain never shows it as running. A mission
        that already moved elsewhere (an operator cancel, a run that did start) is left as it is. If the
        store cannot record this, nothing is claimed: the mission stays awaiting approval for the CLI."""
        code = error.code if isinstance(error, EngineError) else f"internal_error:{type(error).__name__}"
        try:
            row = engine.store.mission(mission_id)
            if row["state"] != MissionState.AWAITING_APPROVAL.value:
                return
            engine.store.transition(mission_id, MissionState.AWAITING_APPROVAL, MissionState.FAILED,
                                    reason=f"run_refused:{code}")
            engine.store.append_event(mission_id, "brain.run_refused",
                                      {"code": code, "scope_sha256": row["plan_sha256"]})
        except Exception:
            return

    def discard(self, mission_id: str) -> dict:
        if not MISSION_ID.fullmatch(mission_id or ""):
            raise CommandRefused(400, "invalid_request")
        with self._lock:
            engine = self._engine()
            try:
                try:
                    state = engine.store.state(mission_id)
                except StoreError:
                    raise CommandRefused(404, "unknown_mission") from None
                if not self._is_brain_draft(engine, mission_id):
                    raise CommandRefused(403, "not_a_brain_draft")
                if state is not MissionState.AWAITING_APPROVAL:
                    raise CommandRefused(409, "not_awaiting_approval")
                engine.cancel(mission_id, reason="discarded_from_brain")
            finally:
                engine.close()
        return {"mission_id": mission_id, "state": "cancelled"}

    def wait(self, timeout: float = 120.0) -> None:
        """Tests: wait for background runs to finish."""
        for thread in list(self._runs.values()):
            thread.join(timeout)


def load_template(path: Path) -> dict:
    """Read the operator template; resolve a relative source against its directory."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("template must be a JSON object")
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        raise ValueError("template.workspace must be a JSON object")
    source = workspace.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("template.workspace.source must be a nonempty string")
    if not Path(source).is_absolute():
        workspace["source"] = str((path.parent / source).resolve())
    return data
