"""Mission entry from the Brain (opt-in): draft from words, approve by a deliberate gesture.

Enabled only by ``petron brain serve --interactive --template SPEC``. Authority stays where it was:

* the **operator** chooses the template at the terminal: workspace, tools, writable paths, limits,
  provider and sandbox. The browser can contribute only the objective text;
* a draft is an ordinary agent mission that stops at ``awaiting_approval`` (scope hash recorded);
* approval is a separate, explicit act bound to that scope hash; through the Brain it is allowed
  only for missions the Brain itself drafted (CLI missions keep CLI approval);
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

    def _engine(self) -> Engine:
        return self.engine_factory(self.home)  # one engine (SQLite connection) per call/thread

    def _is_brain_draft(self, engine: Engine, mission_id: str) -> bool:
        return any(e["kind"] == "brain.draft" for e in engine.store.events(mission_id))

    def _active_draft(self, engine: Engine) -> str | None:
        for row in engine.store.missions():
            if row["mission_id"].startswith("brain-") and MissionState(row["state"]) not in _TERMINAL:
                return row["mission_id"]
        return None

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
                try:
                    mission_id = engine.create(spec)
                except (SchemaError, EngineError, StoreError):
                    raise CommandRefused(400, "draft_rejected") from None
                engine.store.append_event(mission_id, "brain.draft", {
                    "channel": "brain", "objective_sha256": hashlib.sha256(objective.encode()).hexdigest()})
                scope_sha256 = engine.plan(mission_id)
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
        return {"mission_id": mission_id, "approved": scope_sha256, "state": "running"}

    def _run(self, mission_id: str) -> None:
        engine = self._engine()
        try:
            engine.run(mission_id, allow_real_model=self.allow_real_model)
        except EngineError:
            pass  # recorded by the engine (or refused before running); the Brain sees the events
        finally:
            engine.close()

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
