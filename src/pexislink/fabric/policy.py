"""Deny-by-default policy engine with expiring, revocable, per-entity grants.

Trust boundary. A ``Principal`` is bound by trusted code when a session is
opened (``Fabric.open_session``), for example from the operator's launch
configuration of the MCP server. Nothing that arrives in a request (tool
arguments, observation payloads, model output) can name, change or extend the
principal or its grants: the request-facing API has no such parameter, and
there is no request-facing operation that writes policy.

Decision order (first failing step decides, every step fails closed):

    1. capability is one of the four read-only capabilities  else capability_not_supported
    2. the entity exists                                     else unknown_entity
    3. the entity declares (supports) the capability         else capability_not_declared
    4. a grant names this principal, entity and capability   else no_grant
    5. that grant is not revoked                              else revoked
    6. now >= not_before                                      else not_yet_valid
    7. now <  expires_at                                      else expired

Any exception inside ``decide`` yields ``policy_error`` (deny).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    POLICY_SCHEMA,
    READ_ONLY_CAPABILITIES,
    ContractError,
    canonical_json,
    check_capability,
    check_depth,
    check_id,
    closed,
    format_time,
    parse_time,
)

Clock = Callable[[], int]  # integer microseconds since the epoch, UTC


def system_clock() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


class ManualClock:
    """Deterministic clock for tests, replay and the demo."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1_000_000)


@dataclass(frozen=True)
class Principal:
    principal_id: str

    def __post_init__(self) -> None:
        check_id(self.principal_id, "principal_id")


@dataclass(frozen=True)
class Grant:
    grant_id: str
    principal_id: str
    entity_id: str          # exact identifier: no wildcards in v0.1
    capabilities: frozenset[str]
    not_before: int
    expires_at: int         # mandatory: a grant that never expires cannot be represented

    def __post_init__(self) -> None:
        check_id(self.grant_id, "grant_id")
        check_id(self.principal_id, "principal_id")
        check_id(self.entity_id, "entity_id")
        if not isinstance(self.capabilities, frozenset) or not self.capabilities:
            raise ContractError("invalid_capabilities", "capabilities")
        for capability in self.capabilities:
            check_capability(capability, "capabilities")
        if not (isinstance(self.not_before, int) and isinstance(self.expires_at, int)
                and self.not_before < self.expires_at):
            raise ContractError("invalid_validity", "expires_at")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    principal_id: str
    entity_id: str
    capability: str
    grant_id: str | None
    decided_at: int


class AuditLog:
    """Bounded, append-only, hash-chained record of decisions and executions.

    Each record carries ``prev`` (the previous record's digest) and ``digest``,
    so a rewritten or dropped record breaks the chain (``verify``). Records
    contain identifiers and reason codes only, never payload values."""

    def __init__(self, max_records: int = 10_000, sink: Callable[[dict], None] | None = None) -> None:
        self._records: deque[dict] = deque(maxlen=max_records)
        self._lock = threading.Lock()
        self._seq = 0
        self._head = "0" * 64
        self._sink = sink

    def append(self, **fields: Any) -> dict:
        """Append one record and return a copy of it. With a sink, the record is
        committed only after the sink accepts it: if the sink raises, the chain
        does not advance and the exception propagates (callers fail closed)."""
        with self._lock:
            record = {"seq": self._seq + 1, "prev": self._head, **fields}
            record["digest"] = hashlib.sha256(canonical_json(record).encode("ascii")).hexdigest()
            if self._sink is not None:
                self._sink(dict(record))  # under the lock: sink order equals chain order
            self._seq += 1
            self._head = record["digest"]
            self._records.append(record)
            return dict(record)

    def records(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    @staticmethod
    def verify(records: list[dict]) -> bool:
        previous = records[0]["prev"] if records else None
        for record in records:
            body = {k: v for k, v in record.items() if k != "digest"}
            if record["prev"] != previous:
                return False
            if hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest() != record["digest"]:
                return False
            previous = record["digest"]
        return True


@dataclass
class PolicyEngine:
    """Holds grants and revocations. Mutators are operator APIs (trusted code)."""
    clock: Clock = system_clock
    max_grants: int = 1024
    _grants: dict[str, Grant] = field(default_factory=dict)
    _revoked: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_grant(self, grant: Grant) -> None:
        if not isinstance(grant, Grant):
            raise ContractError("invalid_grant", "grant")
        with self._lock:
            if grant.grant_id in self._grants or grant.grant_id in self._revoked:
                raise ContractError("duplicate_grant", "grant_id")  # a revoked id is never reused
            if len(self._grants) >= self.max_grants:
                raise ContractError("too_many_grants", "grants")
            self._grants[grant.grant_id] = grant

    def revoke(self, grant_id: str) -> bool:
        """Revocation is immediate for every later decision. Returns whether the id existed."""
        with self._lock:
            self._revoked.add(grant_id)
            return grant_id in self._grants

    def decide(self, principal: Principal, entity_id: str, capability: str,
               declared: Callable[[str], frozenset[str] | None]) -> Decision:
        """``declared(entity_id)`` returns the entity's declared capabilities, or
        None when the entity does not exist."""
        principal_id = getattr(principal, "principal_id", "?")
        now = 0
        try:
            now = self.clock()
            if not isinstance(principal, Principal):
                return self._deny("invalid_principal", principal_id, entity_id, capability, now)
            if not isinstance(capability, str) or capability not in READ_ONLY_CAPABILITIES:
                return self._deny("capability_not_supported", principal_id, entity_id, capability, now)
            supported = declared(entity_id)
            if supported is None:
                return self._deny("unknown_entity", principal_id, entity_id, capability, now)
            if capability not in supported:
                return self._deny("capability_not_declared", principal_id, entity_id, capability, now)
            with self._lock:
                candidates = [g for g in self._grants.values() if g.principal_id == principal_id
                              and g.entity_id == entity_id and capability in g.capabilities]
                revoked = set(self._revoked)
            if not candidates:
                return self._deny("no_grant", principal_id, entity_id, capability, now)
            failures = set()
            for grant in sorted(candidates, key=lambda g: g.grant_id):
                if grant.grant_id in revoked:
                    failures.add("revoked")
                elif now < grant.not_before:
                    failures.add("not_yet_valid")
                elif now >= grant.expires_at:
                    failures.add("expired")
                else:
                    return Decision(True, "granted", principal_id, entity_id, capability, grant.grant_id, now)
            reason = next(r for r in ("revoked", "expired", "not_yet_valid") if r in failures)
            return self._deny(reason, principal_id, entity_id, capability, now)
        except Exception:  # fail closed on any internal fault
            return self._deny("policy_error", principal_id, entity_id, capability, now)

    @staticmethod
    def _deny(reason: str, principal_id: Any, entity_id: Any, capability: Any, now: int) -> Decision:
        return Decision(False, reason, str(principal_id)[:128], str(entity_id)[:128], str(capability)[:64], None, now)


# --- Operator-side policy file (closed schema, bounded, no symlinks) ---------

_POLICY_KEYS = frozenset({"schema_version", "grants"})
_GRANT_KEYS = frozenset({"grant_id", "principal_id", "entity_id", "capabilities", "not_before", "expires_at"})
MAX_POLICY_BYTES = 64 * 1024


def parse_policy(document: Any, max_grants: int = 1024) -> list[Grant]:
    check_depth(document, 4, "policy")
    doc = closed(document, "policy", _POLICY_KEYS)
    if doc["schema_version"] != POLICY_SCHEMA:
        raise ContractError("unsupported_schema_version", "schema_version")
    if not isinstance(doc["grants"], list) or len(doc["grants"]) > max_grants:
        raise ContractError("invalid_grants", "grants")
    grants = []
    for raw in doc["grants"]:
        g = closed(raw, "grant", _GRANT_KEYS)
        caps = g["capabilities"]
        if not isinstance(caps, list) or not caps or len(caps) > len(READ_ONLY_CAPABILITIES) \
                or not all(isinstance(c, str) for c in caps) or len(set(caps)) != len(caps):
            raise ContractError("invalid_capabilities", "grant.capabilities")
        for capability in caps:
            check_capability(capability, "grant.capabilities")
        grants.append(Grant(check_id(g["grant_id"], "grant.grant_id"), g["principal_id"], g["entity_id"],
                            frozenset(caps), parse_time(g["not_before"], "grant.not_before"),
                            parse_time(g["expires_at"], "grant.expires_at")))
    if len({g.grant_id for g in grants}) != len(grants):
        raise ContractError("duplicate_grant", "grants")
    return grants


def grants_to_document(grants: Iterable[Grant]) -> dict:
    return {"schema_version": POLICY_SCHEMA, "grants": [
        {"grant_id": g.grant_id, "principal_id": g.principal_id, "entity_id": g.entity_id,
         "capabilities": sorted(g.capabilities), "not_before": format_time(g.not_before),
         "expires_at": format_time(g.expires_at)} for g in grants]}


def load_policy_file(path: str | os.PathLike, max_bytes: int = MAX_POLICY_BYTES) -> list[Grant]:
    """Read an operator policy file: regular file only, symlinks refused at the
    final component (O_NOFOLLOW), size capped, identity re-checked after the
    read (TOCTOU). Errors carry codes only, never the path or the content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        before = os.lstat(path)
    except OSError:
        raise ContractError("policy_unreadable") from None
    if stat.S_ISLNK(before.st_mode):
        raise ContractError("policy_symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ContractError("policy_not_regular_file")
    if before.st_size > max_bytes:
        raise ContractError("policy_too_large")
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ContractError("policy_unreadable") from None
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(before, opened) or not stat.S_ISREG(opened.st_mode) \
                or opened.st_ctime_ns != before.st_ctime_ns:
            raise ContractError("policy_changed")
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ContractError("policy_too_large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        # ctime cannot be set back by a writer (unlike mtime via utime): it exposes same-inode rewrites
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns,
                                                                     opened.st_ctime_ns) or total != opened.st_size:
            raise ContractError("policy_changed")
    finally:
        os.close(descriptor)
    try:
        current = os.lstat(path)
    except OSError:
        raise ContractError("policy_changed") from None
    if not os.path.samestat(before, current) or current.st_ctime_ns != before.st_ctime_ns:
        raise ContractError("policy_changed")
    try:
        document = json.loads(b"".join(chunks).decode("utf-8"),
                              parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ContractError("policy_invalid_json") from None
    return parse_policy(document)


def _reject_constant(_: str) -> Any:
    raise ContractError("policy_invalid_json")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ContractError("policy_duplicate_key")
        out[key] = value
    return out


__all__ = ["AuditLog", "Clock", "Decision", "Grant", "ManualClock", "PolicyEngine", "Principal",
           "grants_to_document", "load_policy_file", "parse_policy", "system_clock"]
