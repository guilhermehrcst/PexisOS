"""Tool registry and Tool Gateway: the only path from a plan step to an effect.

Each call is checked, in order, against: the registry (unknown tools are refused), the tool's
closed input schema, the mission permissions from the operator's spec, the tool-call budget,
the mission state (must be ``running``), and the tool's effect class:

* ``read``            — confined to the disposable workspace;
* ``write_workspace`` — only paths listed as writable and not protected;
* ``execute``         — only through a sandbox whose isolation probe passed, and only while
                        a recorded approval matches the mission's current plan hash;
* ``external_read``   — only through a configured read-only connector.

The mission context is built by trusted code (the engine) from the stored spec. Nothing in the
tool arguments can name a principal, a permission or a path outside the workspace: every
schema is closed. Every decision and every result digest is written to the event log.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .mission import VERIFICATION_COMMANDS, MissionSpec, MissionState, canonical_json, is_writable
from .sandbox import SandboxUnavailable
from .schema import SchemaError, closed_object, validate
from .store import Store
from .workspace import Workspace, WorkspaceError

READ, WRITE, EXECUTE, EXTERNAL_READ = "read", "write_workspace", "execute", "external_read"
_PATH = {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$"}
_FABRIC_ENTITY = {"type": "string", "minLength": 3, "maxLength": 128, "pattern": r"^[a-z][a-z0-9:._-]*$"}
_FABRIC_CURSOR = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": r"^[a-z0-9:._-]+$"}
_FABRIC_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: str
    description: str
    input_schema: dict
    handler: Callable[["MissionContext", dict], dict]


@dataclass
class MissionContext:
    """Built by the engine from trusted state. Never built from provider output."""
    store: Store
    spec: MissionSpec
    workspace: Workspace
    sandbox: Any
    plan_sha256: str
    connector: Any = None
    tool_calls: int = 0
    notes: dict = field(default_factory=dict)


class ToolDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _list_files(ctx: MissionContext, args: dict) -> dict:
    return {"files": ctx.workspace.list_files()}


def _read_file(ctx: MissionContext, args: dict) -> dict:
    return {"path": args["path"], "content": ctx.workspace.read_text(args["path"])}


def _replace_in_file(ctx: MissionContext, args: dict) -> dict:
    ctx.workspace.replace_once(args["path"], args["old"], args["new"])
    return {"path": args["path"], "replaced": 1}


def _run_tests(ctx: MissionContext, args: dict) -> dict:
    argv = VERIFICATION_COMMANDS[ctx.spec.raw["verification"]["command"]]
    return ctx.sandbox.run(ctx.workspace.root, argv, ctx.spec.budget["exec_timeout_s"]).to_dict()


def _fabric(name: str) -> Callable[[MissionContext, dict], dict]:
    def handler(ctx: MissionContext, args: dict) -> dict:
        return ctx.connector.call(name, args)
    return handler


_ENTITY_ONLY = closed_object({"entity_id": _FABRIC_ENTITY}, ["entity_id"])

BUILTIN_TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (
    ToolSpec("list_files", READ, "List files in the disposable workspace.", closed_object({}), _list_files),
    ToolSpec("read_file", READ, "Read one UTF-8 file (max 64 KiB) from the workspace.",
             closed_object({"path": _PATH}, ["path"]), _read_file),
    ToolSpec("replace_in_file", WRITE, "Replace exactly one occurrence of `old` with `new` in a writable file.",
             closed_object({"path": _PATH, "old": {"type": "string", "minLength": 1, "maxLength": 20000},
                            "new": {"type": "string", "maxLength": 20000}}, ["path", "old", "new"]),
             _replace_in_file),
    ToolSpec("run_tests", EXECUTE, "Run the mission's verification command inside the isolated sandbox.",
             closed_object({}), _run_tests),
    ToolSpec("pexisos.list_entities", EXTERNAL_READ, "PexisOS Agent Fabric (read-only): list entities.",
             closed_object({"cursor": _FABRIC_CURSOR, "limit": _FABRIC_LIMIT}), _fabric("list_entities")),
    ToolSpec("pexisos.describe_entity", EXTERNAL_READ, "PexisOS Agent Fabric (read-only): describe an entity.",
             _ENTITY_ONLY, _fabric("describe_entity")),
    ToolSpec("pexisos.list_capabilities", EXTERNAL_READ, "PexisOS Agent Fabric (read-only): capabilities.",
             _ENTITY_ONLY, _fabric("list_capabilities")),
    ToolSpec("pexisos.read_observations", EXTERNAL_READ, "PexisOS Agent Fabric (read-only): observations.",
             closed_object({"entity_id": _FABRIC_ENTITY, "mode": {"type": "string", "enum": ["latest", "history"]},
                            "cursor": {"type": "string", "pattern": r"^[0-9]{1,18}$"}, "limit": _FABRIC_LIMIT},
                           ["entity_id", "mode"]), _fabric("read_observations")),
    ToolSpec("pexisos.explain_assessment", EXTERNAL_READ, "PexisOS Agent Fabric (read-only): explained inference.",
             closed_object({"entity_id": _FABRIC_ENTITY,
                            "signal": {"type": "string", "minLength": 1, "maxLength": 48,
                                       "pattern": r"^[a-z][a-z0-9_]*$"}}, ["entity_id"]),
             _fabric("explain_assessment")),
)}


def tool_schemas(registry: dict[str, ToolSpec] = BUILTIN_TOOLS) -> dict[str, dict]:
    return {name: spec.input_schema for name, spec in registry.items()}


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class Gateway:
    def __init__(self, registry: dict[str, ToolSpec] | None = None):
        self.registry = dict(BUILTIN_TOOLS if registry is None else registry)

    def _authorize(self, ctx: MissionContext, tool: Any, args: Any) -> ToolSpec:
        if not isinstance(tool, str) or tool not in self.registry:
            raise ToolDenied("unknown_tool")
        spec = self.registry[tool]
        try:
            validate(spec.input_schema, args)
        except SchemaError:
            raise ToolDenied("invalid_arguments") from None
        if tool not in ctx.spec.tools:
            raise ToolDenied("tool_not_permitted")
        if ctx.tool_calls >= ctx.spec.budget["max_tool_calls"]:
            raise ToolDenied("tool_budget_exhausted")
        if ctx.store.state(ctx.spec.mission_id) is not MissionState.RUNNING:
            raise ToolDenied("mission_not_running")
        if spec.effect == WRITE and not is_writable(ctx.spec, args["path"]):
            raise ToolDenied("path_not_writable")
        if spec.effect == EXECUTE:
            if ctx.store.approval_for(ctx.spec.mission_id, ctx.plan_sha256) is None:
                raise ToolDenied("approval_missing")
            if ctx.store.mission(ctx.spec.mission_id)["plan_sha256"] != ctx.plan_sha256:
                raise ToolDenied("plan_changed")
        if spec.effect == EXTERNAL_READ and ctx.connector is None:
            raise ToolDenied("connector_not_configured")
        return spec

    def call(self, ctx: MissionContext, tool: Any, args: Any) -> dict:
        """Returns ``{"ok": True, "result": ...}`` or ``{"ok": False, "error": code}``; never raises
        for a denied or failed tool, and records both outcomes."""
        mission_id = ctx.spec.mission_id
        label = tool if isinstance(tool, str) and tool in self.registry else "<unknown>"
        try:
            spec = self._authorize(ctx, tool, args)
        except ToolDenied as denied:
            ctx.store.append_event(mission_id, "tool.authorization",
                                   {"tool": label, "decision": "deny", "reason": denied.reason})
            return {"ok": False, "error": denied.reason}
        ctx.tool_calls += 1
        ctx.store.append_event(mission_id, "tool.authorization",
                               {"tool": label, "decision": "allow", "effect": spec.effect,
                                "args_sha256": _digest(args), "call": ctx.tool_calls})
        try:
            result = spec.handler(ctx, args)
        except WorkspaceError as error:
            outcome = {"ok": False, "error": error.code}
        except SandboxUnavailable as error:
            outcome = {"ok": False, "error": f"isolation_unavailable:{error.reason}"}
        except Exception as error:  # connector or handler failure: fail closed, keep only a code or the type
            code = getattr(error, "code", None)
            outcome = {"ok": False, "error": f"tool_failed:{code if isinstance(code, str) else type(error).__name__}"}
        else:
            outcome = {"ok": True, "result": result}
        ctx.store.append_event(mission_id, "tool.result",
                               {"tool": label, "ok": outcome["ok"], "error": outcome.get("error"),
                                "result_sha256": _digest(outcome.get("result")),
                                **({"exit_code": result.get("exit_code")} if outcome["ok"] and spec.effect == EXECUTE
                                   else {})})
        return outcome
