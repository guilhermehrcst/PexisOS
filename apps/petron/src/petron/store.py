"""SQLite persistence: missions, append-only hash-chained events, approvals, artifacts.

Invariants enforced inside SQLite (independent of the Python checks, so direct SQL cannot
bypass them either):

* a mission's state changes only along ``TRANSITIONS``;
* a mission's spec and id never change, and its plan is written at most once;
* events, approvals and artifacts are append-only (no UPDATE, no DELETE).

State changes are compare-and-set: two processes racing to run the same mission cannot both
win. Every string payload is redacted before it is written.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import secrets
import socket
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from .mission import TRANSITIONS, MissionSpec, MissionState, canonical_json, check_transition
from .redact import contains_secret, redact

GENESIS = "0" * 64


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _transition_trigger() -> str:
    pairs = ", ".join(f"'{a.value}>{b.value}'" for a, targets in TRANSITIONS.items() for b in sorted(targets))
    return (f"CREATE TRIGGER IF NOT EXISTS missions_state_guard BEFORE UPDATE OF state ON missions "
            f"WHEN (OLD.state || '>' || NEW.state) NOT IN ({pairs}) "
            f"BEGIN SELECT RAISE(ABORT, 'illegal_transition'); END;")


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS missions (
        mission_id TEXT PRIMARY KEY,
        spec_json TEXT NOT NULL,
        spec_sha256 TEXT NOT NULL,
        state TEXT NOT NULL,
        plan_json TEXT,
        plan_sha256 TEXT,
        failure_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id TEXT,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id TEXT NOT NULL REFERENCES missions(mission_id),
        plan_sha256 TEXT NOT NULL,
        approver TEXT NOT NULL,
        channel TEXT NOT NULL,
        at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS artifacts (
        mission_id TEXT NOT NULL REFERENCES missions(mission_id),
        name TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (mission_id, name))""",
    _transition_trigger(),
    """CREATE TRIGGER IF NOT EXISTS missions_identity_guard BEFORE UPDATE OF mission_id, spec_json, spec_sha256,
        created_at ON missions BEGIN SELECT RAISE(ABORT, 'immutable_spec'); END""",
    """CREATE TRIGGER IF NOT EXISTS missions_plan_guard BEFORE UPDATE OF plan_json, plan_sha256 ON missions
        WHEN OLD.plan_json IS NOT NULL OR OLD.state != 'planning'
        BEGIN SELECT RAISE(ABORT, 'immutable_plan'); END""",
    "CREATE TRIGGER IF NOT EXISTS missions_no_delete BEFORE DELETE ON missions BEGIN SELECT RAISE(ABORT, 'append_only'); END",
]
for _table in ("events", "approvals", "artifacts"):
    _SCHEMA.append(f"CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table} "
                   f"BEGIN SELECT RAISE(ABORT, 'append_only'); END")
    _SCHEMA.append(f"CREATE TRIGGER IF NOT EXISTS {_table}_no_delete BEFORE DELETE ON {_table} "
                   f"BEGIN SELECT RAISE(ABORT, 'append_only'); END")


class StoreError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _event_hash(prev_hash: str, seq: int, mission_id: str | None, at: str, kind: str, payload_json: str) -> str:
    material = canonical_json([prev_hash, seq, mission_id, at, kind, payload_json])
    return hashlib.sha256(material.encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], str] = utc_now):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        if fresh:
            os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        for statement in _SCHEMA:
            self._db.execute(statement)

    def close(self) -> None:
        self._db.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        self._write_anchor()

    # head anchor --------------------------------------------------------------------------
    @property
    def anchor_path(self) -> Path:
        return self.path.with_name(self.path.name + ".anchor")

    def head(self) -> dict | None:
        row = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return {"seq": row["seq"], "hash": row["hash"]} if row else None

    def _read_anchor(self) -> dict | None:
        try:
            data = json.loads(self.anchor_path.read_text())
            return data if isinstance(data.get("seq"), int) and isinstance(data.get("hash"), str) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _write_anchor(self) -> None:
        """Remember the newest committed head beside the database (monotonic, atomic). It is a
        local convenience that catches truncation by anyone who did not also edit this file; it is
        not an independent trust anchor."""
        head = self.head()
        if head is None:
            return
        current = self._read_anchor()
        if current is not None and current["seq"] >= head["seq"]:
            return
        temporary = self.anchor_path.with_name(self.anchor_path.name + f".{os.getpid()}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(canonical_json(head))
            os.replace(temporary, self.anchor_path)
        except OSError:
            pass   # the anchor is best effort; the chain itself is unaffected

    # events -------------------------------------------------------------------------------
    def _append(self, db: sqlite3.Connection, mission_id: str | None, kind: str, payload: dict) -> int:
        row = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash, seq = (row["hash"], row["seq"] + 1) if row else (GENESIS, 1)
        at = self.clock()
        payload_json = canonical_json(redact(payload))
        digest = _event_hash(prev_hash, seq, mission_id, at, kind, payload_json)
        db.execute("INSERT INTO events (seq, mission_id, at, kind, payload_json, prev_hash, hash) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (seq, mission_id, at, kind, payload_json, prev_hash, digest))
        return seq

    def append_event(self, mission_id: str | None, kind: str, payload: dict) -> int:
        with self._tx() as db:
            return self._append(db, mission_id, kind, payload)

    def events(self, mission_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM events" + (" WHERE mission_id = ?" if mission_id else "") + " ORDER BY seq"
        rows = self._db.execute(query, (mission_id,) if mission_id else ()).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def verify_chain(self, expected_head: dict | None = None) -> tuple[bool, int | None]:
        """(ok, first bad seq). Detects edits, deletions and reordering of stored events.

        An unkeyed chain cannot see removal of the *newest* events: what remains is a valid prefix.
        So the head is also compared with the local anchor and, when given, ``expected_head``
        ({"seq", "hash"} recorded somewhere independent): the event at that sequence must still
        exist with that hash. Neither makes the log tamper-proof against a rewrite of everything."""
        prev_hash, expected_seq = GENESIS, 1
        hashes: dict[int, str] = {}
        for row in self._db.execute("SELECT * FROM events ORDER BY seq"):
            digest = _event_hash(prev_hash, row["seq"], row["mission_id"], row["at"], row["kind"], row["payload_json"])
            if row["seq"] != expected_seq or row["prev_hash"] != prev_hash or row["hash"] != digest:
                return False, row["seq"]
            hashes[row["seq"]] = row["hash"]
            prev_hash, expected_seq = row["hash"], expected_seq + 1
        for witness in (self._read_anchor(), expected_head):
            if witness is not None and hashes.get(witness["seq"]) != witness["hash"]:
                return False, min(witness["seq"], expected_seq)
        return True, None

    # missions -----------------------------------------------------------------------------
    def create_mission(self, spec: MissionSpec) -> None:
        # Second barrier (the engine checks first): a recognizable credential is never written to
        # missions.spec_json, which every report and `mission show` reads back.
        if contains_secret(spec.raw):
            raise StoreError("secret_in_spec")
        now = self.clock()
        with self._tx() as db:
            if db.execute("SELECT 1 FROM missions WHERE mission_id = ?", (spec.mission_id,)).fetchone():
                raise StoreError("mission_exists")
            db.execute("INSERT INTO missions (mission_id, spec_json, spec_sha256, state, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (spec.mission_id, canonical_json(spec.raw), spec.sha256, MissionState.CREATED.value, now, now))
            self._append(db, spec.mission_id, "mission.created",
                         {"spec_sha256": spec.sha256, "objective": spec.raw["objective"]})

    def mission(self, mission_id: str) -> dict:
        row = self._db.execute("SELECT * FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
        if row is None:
            raise StoreError("unknown_mission")
        return dict(row)

    def spec(self, mission_id: str) -> MissionSpec:
        row = self.mission(mission_id)
        spec = MissionSpec.parse(json.loads(row["spec_json"]))
        if spec.sha256 != row["spec_sha256"]:
            raise StoreError("spec_integrity")
        return spec

    def state(self, mission_id: str) -> MissionState:
        return MissionState(self.mission(mission_id)["state"])

    def transition(self, mission_id: str, current: MissionState, target: MissionState, *,
                   reason: str | None = None, payload: dict | None = None) -> None:
        """Compare-and-set. Raises ``StoreError('state_conflict')`` if another actor moved first."""
        check_transition(current, target)
        with self._tx() as db:
            cursor = db.execute("UPDATE missions SET state = ?, updated_at = ?, failure_reason = COALESCE(?, "
                                "failure_reason) WHERE mission_id = ? AND state = ?",
                                (target.value, self.clock(), reason, mission_id, current.value))
            if cursor.rowcount != 1:
                raise StoreError("state_conflict")
            self._append(db, mission_id, "mission.state",
                         {"from": current.value, "to": target.value, **({"reason": reason} if reason else {}),
                          **(payload or {})})

    def set_plan(self, mission_id: str, plan: dict, plan_sha256: str, provider_info: dict) -> None:
        with self._tx() as db:
            cursor = db.execute("UPDATE missions SET plan_json = ?, plan_sha256 = ?, updated_at = ? "
                                "WHERE mission_id = ? AND state = 'planning' AND plan_json IS NULL",
                                (canonical_json(plan), plan_sha256, self.clock(), mission_id))
            if cursor.rowcount != 1:
                raise StoreError("plan_conflict")
            self._append(db, mission_id, "plan.proposed", {"plan_sha256": plan_sha256, "provider": provider_info,
                                                           "steps": plan["steps"], "rationale": plan["rationale"]})

    def set_scope(self, mission_id: str, scope: dict, scope_sha256: str) -> None:
        """Agent missions store their scope document where plans live: written once, while
        ``planning``, protected by the same SQL triggers, approved by the same hash check."""
        with self._tx() as db:
            cursor = db.execute("UPDATE missions SET plan_json = ?, plan_sha256 = ?, updated_at = ? "
                                "WHERE mission_id = ? AND state = 'planning' AND plan_json IS NULL",
                                (canonical_json(scope), scope_sha256, self.clock(), mission_id))
            if cursor.rowcount != 1:
                raise StoreError("plan_conflict")
            self._append(db, mission_id, "scope.proposed", {"scope_sha256": scope_sha256, "scope": scope})

    # leases ---------------------------------------------------------------------------------
    def acquire_lease(self, mission_id: str) -> dict:
        """One live runner per mission. A previous holder blocks only while its process is alive
        on this host; a holder on another host always blocks (liveness cannot be checked)."""
        holder = {"pid": os.getpid(), "host": socket.gethostname(), "token": secrets.token_hex(8)}
        with self._tx() as db:
            row = db.execute("SELECT payload_json FROM events WHERE mission_id = ? AND kind = 'run.lease' "
                             "ORDER BY seq DESC LIMIT 1", (mission_id,)).fetchone()
            if row:
                previous = json.loads(row["payload_json"])
                if previous.get("host") != holder["host"]:
                    raise StoreError("lease_held_elsewhere")
                if previous.get("pid") != holder["pid"] and _pid_alive(previous.get("pid")):
                    raise StoreError("lease_held")
            self._append(db, mission_id, "run.lease", holder)
        return holder

    def lease_alive(self, mission_id: str) -> bool | None:
        row = self._db.execute("SELECT payload_json FROM events WHERE mission_id = ? AND kind = 'run.lease' "
                               "ORDER BY seq DESC LIMIT 1", (mission_id,)).fetchone()
        if row is None:
            return None
        previous = json.loads(row["payload_json"])
        if previous.get("host") != socket.gethostname():
            return True
        # A lease held by this very process belongs to a run that already unwound (crashed).
        return previous.get("pid") != os.getpid() and _pid_alive(previous.get("pid"))

    def missions(self) -> list[dict]:
        return [dict(r) for r in self._db.execute(
            "SELECT mission_id, state, failure_reason, created_at, updated_at FROM missions ORDER BY created_at")]

    # approvals ----------------------------------------------------------------------------
    def record_approval(self, mission_id: str, plan_sha256: str, approver: str, channel: str) -> None:
        if contains_secret([approver, channel]):
            raise StoreError("secret_in_approval")
        with self._tx() as db:
            row = db.execute("SELECT state, plan_sha256 FROM missions WHERE mission_id = ?", (mission_id,)).fetchone()
            if row is None:
                raise StoreError("unknown_mission")
            if row["state"] != MissionState.AWAITING_APPROVAL.value:
                raise StoreError("not_awaiting_approval")
            if row["plan_sha256"] != plan_sha256:
                raise StoreError("plan_hash_mismatch")
            db.execute("INSERT INTO approvals (mission_id, plan_sha256, approver, channel, at) VALUES (?, ?, ?, ?, ?)",
                       (mission_id, plan_sha256, approver, channel, self.clock()))
            self._append(db, mission_id, "plan.approved",
                         {"plan_sha256": plan_sha256, "approver": approver, "channel": channel})

    def approval_for(self, mission_id: str, plan_sha256: str) -> dict | None:
        row = self._db.execute("SELECT * FROM approvals WHERE mission_id = ? AND plan_sha256 = ? ORDER BY id LIMIT 1",
                               (mission_id, plan_sha256)).fetchone()
        return dict(row) if row else None

    # artifacts ----------------------------------------------------------------------------
    def put_artifact(self, mission_id: str, name: str, content: str) -> str:
        content = redact(content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._tx() as db:
            db.execute("INSERT INTO artifacts (mission_id, name, sha256, content, at) VALUES (?, ?, ?, ?, ?)",
                       (mission_id, name, digest, content, self.clock()))
            self._append(db, mission_id, "artifact.recorded", {"name": name, "sha256": digest})
        return digest

    def artifact(self, mission_id: str, name: str) -> dict | None:
        row = self._db.execute("SELECT * FROM artifacts WHERE mission_id = ? AND name = ?", (mission_id, name)).fetchone()
        return dict(row) if row else None

    def raw(self) -> sqlite3.Connection:
        """Direct connection, for tests that prove the SQL-level guards hold."""
        return self._db


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
