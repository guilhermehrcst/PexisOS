"""Typed mission: the operator-supplied spec, the state machine and the plan contract.

The spec is trusted input written by the operator (``petron mission create SPEC``). It is the
only source of a mission's identity, permissions, writable paths, verification command and
budget. A provider's plan is untrusted data validated against the spec and the tool registry.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .schema import SchemaError, closed_object, validate

SPEC_SCHEMA_ID = "petron.mission.v0"
PLAN_SCHEMA_ID = "petron.plan.v0"
SCOPE_SCHEMA_ID = "petron.scope.v0"
MODES = ("plan", "agent")
_ID = r"^[a-z0-9][a-z0-9._-]{2,63}$"
_REL_PATH = {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$"}
_TOOL_NAME = {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^[a-z][a-z0-9_.]*$"}
# Verification commands are selected by key; argv is never taken from a spec or a provider.
VERIFICATION_COMMANDS: dict[str, tuple[str, ...]] = {
    "python-unittest": ("python", "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"),
}

SPEC_SCHEMA = closed_object({
    "schema": {"type": "string", "const": SPEC_SCHEMA_ID},
    "mission_id": {"type": "string", "pattern": _ID},
    # "plan" (V0.1, default when absent): one static plan, approved by its hash.
    # "agent" (V0.2): an iterative loop inside an approved, immutable scope.
    "mode": {"type": "string", "enum": list(MODES)},
    "objective": {"type": "string", "minLength": 1, "maxLength": 2000},
    "workspace": closed_object({
        "source": {"type": "string", "minLength": 1, "maxLength": 1024},
        "writable": {"type": "array", "items": _REL_PATH, "minItems": 0, "maxItems": 64},
        "protected": {"type": "array", "items": _REL_PATH, "minItems": 0, "maxItems": 64},
    }, ["source", "writable", "protected"]),
    "permissions": closed_object({
        "tools": {"type": "array", "items": _TOOL_NAME, "maxItems": 32},
    }, ["tools"]),
    "verification": closed_object({
        "command": {"type": "string", "enum": sorted(VERIFICATION_COMMANDS)},
    }, ["command"]),
    "budget": closed_object({
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 64},
        "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 256},
        "max_model_calls": {"type": "integer", "minimum": 0, "maximum": 8},
        "exec_timeout_s": {"type": "integer", "minimum": 1, "maximum": 600},
        "max_iterations": {"type": "integer", "minimum": 1, "maximum": 64},
        "max_total_tokens": {"type": "integer", "minimum": 1000, "maximum": 2000000},
    }, ["max_steps", "max_tool_calls", "max_model_calls", "exec_timeout_s"]),
    "provider": closed_object({
        "kind": {"type": "string", "enum": ["deterministic", "anthropic"]},
        "script": {"type": "string", "pattern": r"^[a-z0-9_]{1,48}$"},
        "model": {"type": "string", "pattern": r"^[a-z0-9.-]{3,64}$"},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
        "max_tokens": {"type": "integer", "minimum": 256, "maximum": 32000},
    }, ["kind"]),
    "sandbox": closed_object({
        "backend": {"type": "string", "enum": ["docker", "none"]},
        "image": {"type": "string", "pattern": r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$"},
    }, ["backend"]),
    # Optional read-only PexisOS Agent Fabric connector; the principal is chosen here, by the operator.
    "pexisos": closed_object({
        "principal": {"type": "string", "minLength": 3, "maxLength": 128, "pattern": r"^[a-z][a-z0-9:._-]*$"},
    }, ["principal"]),
}, ["schema", "mission_id", "objective", "workspace", "permissions", "verification", "budget", "provider",
    "sandbox"])


class MissionState(str, enum.Enum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = MissionState
TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})
TRANSITIONS: dict[MissionState, frozenset[MissionState]] = {
    S.CREATED: frozenset({S.PLANNING, S.CANCELLED}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.FAILED, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.VERIFYING, S.FAILED, S.CANCELLED}),
    S.VERIFYING: frozenset({S.COMPLETED, S.FAILED, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


def check_transition(current: MissionState, target: MissionState) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(current.value, target.value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _check_rel_path(path: str) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("..", ".", "") for part in path.split("/")):
        raise SchemaError("workspace", f"path must be relative and normalized: {path}")


@dataclass(frozen=True)
class MissionSpec:
    raw: dict

    @classmethod
    def parse(cls, data: Any) -> "MissionSpec":
        validate(SPEC_SCHEMA, data)
        for path in [*data["workspace"]["writable"], *data["workspace"]["protected"]]:
            _check_rel_path(path)
        for writable in data["workspace"]["writable"]:
            if any(_covers(protected, writable) for protected in data["workspace"]["protected"]):
                raise SchemaError("workspace.writable", f"writable path is protected: {writable}")
        provider = data["provider"]
        if provider["kind"] == "deterministic" and "script" not in provider:
            raise SchemaError("provider.script", "deterministic provider needs a script")
        if provider["kind"] == "anthropic" and data["budget"]["max_model_calls"] < 1:
            raise SchemaError("budget.max_model_calls", "a real provider needs a model-call budget")
        if data.get("mode") == "agent" and "max_iterations" not in data["budget"]:
            raise SchemaError("budget.max_iterations", "agent mode needs an iteration limit")
        if data["sandbox"]["backend"] == "docker" and "image" not in data["sandbox"]:
            raise SchemaError("sandbox.image", "docker backend needs an image pinned by digest")
        return cls(json.loads(canonical_json(data)))

    @property
    def mission_id(self) -> str:
        return self.raw["mission_id"]

    @property
    def mode(self) -> str:
        return self.raw.get("mode", "plan")

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self.raw["permissions"]["tools"])

    @property
    def writable(self) -> tuple[str, ...]:
        return tuple(self.raw["workspace"]["writable"])

    @property
    def protected(self) -> tuple[str, ...]:
        return tuple(self.raw["workspace"]["protected"])

    @property
    def budget(self) -> dict:
        return dict(self.raw["budget"])

    @property
    def sha256(self) -> str:
        return sha256_json(self.raw)


def _covers(prefix: str, path: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def is_protected(spec: MissionSpec, path: str) -> bool:
    return any(_covers(protected, path) for protected in spec.protected)


def is_writable(spec: MissionSpec, path: str) -> bool:
    return path in spec.writable and not is_protected(spec, path)


class PlanRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_plan(plan: Any, spec: MissionSpec, tool_schemas: dict[str, dict]) -> dict:
    """Return the canonical plan or raise ``PlanRejected``. Nothing in a plan widens the spec."""
    if not isinstance(plan, dict) or set(plan) != {"schema", "rationale", "steps"}:
        raise PlanRejected("plan_shape")
    if plan["schema"] != PLAN_SCHEMA_ID:
        raise PlanRejected("plan_schema")
    if not isinstance(plan["rationale"], str) or len(plan["rationale"]) > 4000:
        raise PlanRejected("plan_rationale")
    steps = plan["steps"]
    if not isinstance(steps, list) or not steps or len(steps) > spec.budget["max_steps"]:
        raise PlanRejected("plan_step_count")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {"tool", "args"}:
            raise PlanRejected(f"step_shape:{index}")
        tool = step["tool"]
        if tool not in tool_schemas:
            raise PlanRejected(f"unknown_tool:{index}")
        if tool not in spec.tools:
            raise PlanRejected(f"tool_not_permitted:{index}")
        try:
            validate(tool_schemas[tool], step["args"])
        except SchemaError as error:
            raise PlanRejected(f"invalid_arguments:{index}:{error.path}") from None
    return json.loads(canonical_json(plan))


def scope_document(spec: MissionSpec) -> dict:
    """What an operator approves for an agent mission: the whole authority the loop may use.

    Derived only from the immutable spec, so its hash changes if any capability, path, limit,
    budget, provider or sandbox setting changes. The loop never receives more than this.
    """
    raw = spec.raw
    return {"schema": SCOPE_SCHEMA_ID, "mission_id": spec.mission_id, "spec_sha256": spec.sha256,
            "mode": "agent", "objective": raw["objective"], "tools": sorted(spec.tools),
            "writable": list(spec.writable), "protected": list(spec.protected),
            "verification": raw["verification"], "budget": raw["budget"], "provider": raw["provider"],
            "sandbox": raw["sandbox"], "pexisos": raw.get("pexisos")}
