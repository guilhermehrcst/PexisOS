"""Versioned, closed contracts of the Agent Fabric (standard library only).

Every value that crosses a boundary (adapter -> core, core -> tool caller) is
validated here against a closed schema: unknown keys, wrong types, non-finite
numbers, oversize strings and over-deep objects are rejected with a stable
error code. Error messages never echo the offending value.

Read-only capability vocabulary. ``observe``, ``describe``, ``read_history``
and ``assess`` are the only capabilities the core can represent. Any other name
(``write``, ``transmit``, ``control``, ...) is rejected as
``capability_not_supported`` wherever it appears: in an entity declaration, in
a grant, or in a request.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

OBSERVATION_SCHEMA = "pexislink.observation.v0"
ENTITY_SCHEMA = "pexislink.entity.v0"
ASSESSMENT_SCHEMA = "pexislink.assessment.v0"
POLICY_SCHEMA = "pexislink.policy.v0"


class Capability(str, Enum):
    OBSERVE = "observe"            # read the latest observation of an entity
    DESCRIBE = "describe"          # read the entity's identity, signals and declared capabilities
    READ_HISTORY = "read_history"  # page through stored observations
    ASSESS = "assess"              # compute an explainable, evidence-qualified assessment


READ_ONLY_CAPABILITIES = frozenset(c.value for c in Capability)


class EvidenceState(str, Enum):
    """How much a piece of data can be trusted. Used by observations (observed,
    late, unknown), by ingest (rejected) and by assessments (insufficient)."""
    OBSERVED = "observed"          # arrived in order and on time, every value inside its declared range
    LATE = "late"                  # valid values, but arrived after the entity's latency budget or out of order
    UNKNOWN = "unknown"            # arrived, but carries no trustworthy measurement (unknown frame, out of range)
    REJECTED = "rejected"          # failed the contract; never stored, only counted and audited
    INSUFFICIENT = "insufficient"  # not enough trustworthy evidence to conclude anything


OBSERVATION_STATUSES = (EvidenceState.OBSERVED.value, EvidenceState.LATE.value, EvidenceState.UNKNOWN.value)
ADAPTER_FLAGS = frozenset({"out_of_range", "unknown_frame", "length_mismatch"})  # what an adapter may report
TIMING_FLAGS = frozenset({"late_arrival", "out_of_order"})                     # derived by the core only
QUALITY_FLAGS = ADAPTER_FLAGS | TIMING_FLAGS
PROVENANCE_SOURCES = frozenset({"synthetic_fixture"})
PROVENANCE_LICENSES = frozenset({"project_original"})
SIGNAL_TYPES = frozenset({"number", "integer", "boolean", "enum"})

# Grammars. Identifiers are lowercase, colon-separated, and cannot contain
# '/', '\\', '..' at a segment start, whitespace or control characters.
_ID = re.compile(r"[a-z][a-z0-9-]{0,31}(?::[a-z0-9][a-z0-9._-]{0,63}){1,3}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,3}")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TEXT = re.compile(r"[A-Za-z0-9 .,()/%+_-]{1,96}")
_UNIT = re.compile(r"[A-Za-z0-9/%._-]{1,16}")
_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z")


class ContractError(ValueError):
    """A value violated a contract. ``code`` is stable; ``field`` names the
    schema position (never the value)."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(f"{code}: {field}" if field else code)
        self.code = code
        self.field = field


def check_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ContractError("invalid_identifier", field)
    return value


def check_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ContractError("invalid_name", field)
    return value


def check_capability(value: Any, field: str) -> Capability:
    if not isinstance(value, str) or value not in READ_ONLY_CAPABILITIES:
        raise ContractError("capability_not_supported", field)
    return Capability(value)


def parse_time(value: Any, field: str) -> int:
    """RFC 3339 UTC ('Z' only, at most microseconds) -> integer microseconds since the epoch."""
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ContractError("invalid_timestamp", field)
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        raise ContractError("invalid_timestamp", field) from None
    fraction = (match.group(7) or "").ljust(6, "0")
    return int(moment.timestamp()) * 1_000_000 + int(fraction or 0)


def format_time(micros: int) -> str:
    seconds, fraction = divmod(micros, 1_000_000)
    text = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{text}.{fraction:06d}Z" if fraction else f"{text}Z"


def closed(obj: Any, field: str, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(obj, dict):
        raise ContractError("invalid_type", field)
    keys = set(obj)
    if not all(isinstance(k, str) for k in keys):
        raise ContractError("invalid_key", field)
    if keys - required - optional:
        raise ContractError("unknown_field", field)
    if required - keys:
        raise ContractError("missing_field", field)
    return obj


def check_depth(obj: Any, limit: int, field: str = "") -> None:
    """Iterative depth check: a hostile object cannot exhaust the recursion limit."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > limit:
                raise ContractError("too_deep", field)
            children = value.values() if isinstance(value, dict) else value
            stack.extend((child, depth + 1) for child in children)


def canonical_json(obj: Any) -> str:
    """Deterministic serialization: sorted keys, no whitespace, ASCII, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class SignalSpec:
    """A declared signal. The core knows nothing about any application domain:
    it only checks types and the declared plausible range."""
    name: str
    type: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    values: tuple[str, ...] = ()   # enum vocabulary; free text never enters the fabric
    assessable: bool = True        # False for counters and identifiers whose level carries no health meaning

    def __post_init__(self) -> None:
        check_name(self.name, "signal.name")
        if not isinstance(self.assessable, bool):
            raise ContractError("invalid_signal", "signal.assessable")
        if self.type not in SIGNAL_TYPES:
            raise ContractError("invalid_signal_type", "signal.type")
        if self.unit is not None and (not isinstance(self.unit, str) or not _UNIT.fullmatch(self.unit)):
            raise ContractError("invalid_unit", "signal.unit")
        numeric = self.type in ("number", "integer")
        if numeric != (self.minimum is not None and self.maximum is not None):
            raise ContractError("invalid_range", "signal.range")
        if numeric and not (_finite(self.minimum) and _finite(self.maximum) and self.minimum < self.maximum):
            raise ContractError("invalid_range", "signal.range")
        if (self.type == "enum") != bool(self.values):
            raise ContractError("invalid_enum", "signal.values")
        if self.type == "enum" and (len(self.values) > 16 or len(set(self.values)) != len(self.values)
                                    or not all(isinstance(v, str) and _NAME.fullmatch(v) for v in self.values)):
            raise ContractError("invalid_enum", "signal.values")

    def accepts_type(self, value: Any) -> bool:
        if self.type == "boolean":
            return isinstance(value, bool)
        if self.type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and _finite(value)
        return isinstance(value, str)

    def in_range(self, value: Any) -> bool:
        if self.type in ("number", "integer"):
            return self.minimum <= value <= self.maximum
        if self.type == "enum":
            return value in self.values
        return True

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "unit": self.unit}
        if self.type in ("number", "integer"):
            out["range"] = {"minimum": self.minimum, "maximum": self.maximum}
        if self.type == "enum":
            out["values"] = list(self.values)
        out["assessable"] = self.assessable
        return out


@dataclass(frozen=True)
class EntitySpec:
    """Identity and declared (supported) capabilities of one observed entity.

    ``capabilities`` says what the entity SUPPORTS. Whether a principal is
    AUTHORIZED to use one is the policy engine's decision, never this record's."""
    entity_id: str
    kind: str
    display_name: str
    adapter: str
    capabilities: frozenset[str]
    signals: tuple[SignalSpec, ...]
    expected_interval_s: float
    max_latency_s: float
    stale_after_s: float

    def __post_init__(self) -> None:
        check_id(self.entity_id, "entity_id")
        check_id(self.adapter, "adapter")
        if not isinstance(self.kind, str) or not _KIND.fullmatch(self.kind):
            raise ContractError("invalid_kind", "kind")
        if not isinstance(self.display_name, str) or not _TEXT.fullmatch(self.display_name):
            raise ContractError("invalid_text", "display_name")
        if not isinstance(self.capabilities, frozenset) or not self.capabilities:
            raise ContractError("invalid_capabilities", "capabilities")
        for capability in self.capabilities:
            check_capability(capability, "capabilities")
        if not self.signals or len(self.signals) > 32:
            raise ContractError("invalid_signals", "signals")
        if len({s.name for s in self.signals}) != len(self.signals):
            raise ContractError("duplicate_signal", "signals")
        for value, field in ((self.expected_interval_s, "expected_interval_s"), (self.max_latency_s, "max_latency_s"),
                             (self.stale_after_s, "stale_after_s")):
            if not (_finite(value) and 0 < value <= 31 * 86400):
                raise ContractError("invalid_duration", field)

    def signal(self, name: str) -> SignalSpec | None:
        return next((s for s in self.signals if s.name == name), None)

    def to_dict(self) -> dict:
        return {
            "schema_version": ENTITY_SCHEMA, "entity_id": self.entity_id, "kind": self.kind,
            "display_name": self.display_name, "adapter": self.adapter,
            "declared_capabilities": sorted(self.capabilities),
            "signals": [s.to_dict() for s in self.signals],
            "timing": {"expected_interval_s": self.expected_interval_s, "max_latency_s": self.max_latency_s,
                       "stale_after_s": self.stale_after_s},
        }


_OBS_KEYS = frozenset({"schema_version", "event_id", "entity_id", "observed_at", "received_at", "source", "kind",
                       "payload", "quality", "provenance"})
_QUALITY_KEYS = frozenset({"status", "flags"})
_PROV_REQUIRED = frozenset({"source", "license", "generator"})
_PROV_OPTIONAL = frozenset({"seed"})


@dataclass(frozen=True)
class Observation:
    """A validated observation. Times are integer microseconds (UTC)."""
    event_id: str
    entity_id: str
    observed_at: int
    received_at: int
    source: str
    kind: str
    payload: tuple[tuple[str, Any], ...]
    status: str
    flags: tuple[str, ...]
    provenance: tuple[tuple[str, Any], ...]

    def value(self, name: str) -> Any:
        return dict(self.payload).get(name)

    def to_dict(self) -> dict:
        return {
            "schema_version": OBSERVATION_SCHEMA, "event_id": self.event_id, "entity_id": self.entity_id,
            "observed_at": format_time(self.observed_at), "received_at": format_time(self.received_at),
            "source": self.source, "kind": self.kind, "payload": dict(self.payload),
            "quality": {"status": self.status, "flags": list(self.flags)}, "provenance": dict(self.provenance),
        }


def parse_observation(raw: Any, max_depth: int = 4, max_string: int = 64, max_keys: int = 32) -> dict:
    """Structural validation of one raw observation (adapter output is untrusted).

    Returns a normalized dict; semantic checks against the entity's declared
    signals (types, ranges, lateness) are done by the store at ingest."""
    check_depth(raw, max_depth, "observation")
    obs = closed(raw, "observation", _OBS_KEYS)
    if obs["schema_version"] != OBSERVATION_SCHEMA:
        raise ContractError("unsupported_schema_version", "schema_version")
    if not isinstance(obs["event_id"], str) or not _EVENT_ID.fullmatch(obs["event_id"]):
        raise ContractError("invalid_event_id", "event_id")
    check_id(obs["entity_id"], "entity_id")
    check_id(obs["source"], "source")
    if not isinstance(obs["kind"], str) or not _KIND.fullmatch(obs["kind"]):
        raise ContractError("invalid_kind", "kind")
    observed_at = parse_time(obs["observed_at"], "observed_at")
    received_at = parse_time(obs["received_at"], "received_at")
    if received_at < observed_at:
        raise ContractError("received_before_observed", "received_at")
    payload = obs["payload"]
    if not isinstance(payload, dict) or len(payload) > max_keys:
        raise ContractError("invalid_payload", "payload")
    for key, value in payload.items():
        check_name(key, "payload")
        if isinstance(value, str):
            if len(value) > max_string:
                raise ContractError("string_too_long", "payload")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ContractError("non_finite_number", "payload")
        elif isinstance(value, int) and not isinstance(value, bool):
            if abs(value) > 2 ** 53:
                raise ContractError("integer_out_of_range", "payload")
        elif not isinstance(value, bool):
            raise ContractError("invalid_payload_value", "payload")
    quality = closed(obs["quality"], "quality", _QUALITY_KEYS)
    if quality["status"] not in (EvidenceState.OBSERVED.value, EvidenceState.UNKNOWN.value):
        raise ContractError("invalid_quality_status", "quality.status")  # 'late' is derived by the core, never claimed
    flags = quality["flags"]
    if not isinstance(flags, list) or len(flags) > len(ADAPTER_FLAGS) or len(set(map(str, flags))) != len(flags) \
            or not all(isinstance(f, str) and f in ADAPTER_FLAGS for f in flags):
        raise ContractError("invalid_quality_flags", "quality.flags")
    prov = closed(obs["provenance"], "provenance", _PROV_REQUIRED, _PROV_OPTIONAL)
    if prov["source"] not in PROVENANCE_SOURCES:
        raise ContractError("invalid_provenance", "provenance.source")
    if prov["license"] not in PROVENANCE_LICENSES:
        raise ContractError("invalid_provenance", "provenance.license")
    if not isinstance(prov["generator"], str) or not _KIND.fullmatch(prov["generator"]):
        raise ContractError("invalid_provenance", "provenance.generator")
    if "seed" in prov and (not isinstance(prov["seed"], int) or isinstance(prov["seed"], bool)
                           or not 0 <= prov["seed"] < 2 ** 32):
        raise ContractError("invalid_provenance", "provenance.seed")
    return {"event_id": obs["event_id"], "entity_id": obs["entity_id"], "observed_at": observed_at,
            "received_at": received_at, "source": obs["source"], "kind": obs["kind"], "payload": dict(payload),
            "status": quality["status"], "flags": tuple(flags), "provenance": dict(prov)}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
