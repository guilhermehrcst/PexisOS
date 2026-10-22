"""Iterative agent loop (V0.2): decide → act through the gateway → observe → decide again.

Separation of powers (unchanged from V0.1):

* the provider only *proposes* the next decision, as data;
* the operator approved the mission's scope (``scope_document``) before the loop started;
* the gateway authorizes every tool call against that scope;
* the sandbox is the only place code runs;
* success is decided by the runtime's own verification, never by the provider's words.

Decision contract (``petron.decision.v0``, closed): exactly one of

* ``tool_call``            — run one tool through the gateway;
* ``request_verification`` — ask the runtime to run its independent checks now;
* ``conclude``             — finish; accepted only right after a passing runtime verification
                              on the current workspace state;
* ``request_approval``     — the task needs authority outside the approved scope (handoff);
* ``stop``                 — give up, with a category.

Observations are produced by trusted code from real tool results. A decision cannot carry
results, permissions or identities: unknown fields are rejected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .mission import canonical_json
from .redact import redact
from .workspace import snapshot, tree_sha256

DECISION_SCHEMA_ID = "petron.decision.v0"
KINDS = ("tool_call", "request_verification", "conclude", "request_approval", "stop")
STOP_CATEGORIES = ("impossible", "missing_context", "permission", "limit")
_FIELDS = {
    "tool_call": ({"kind", "rationale", "tool", "args"}, {"kind", "tool", "args"}),
    "request_verification": ({"kind", "rationale", "summary"}, {"kind"}),
    "conclude": ({"kind", "rationale", "summary"}, {"kind", "summary"}),
    "request_approval": ({"kind", "rationale", "reason", "requested_tools", "requested_paths"}, {"kind", "reason"}),
    "stop": ({"kind", "rationale", "reason", "category"}, {"kind", "reason", "category"}),
}
_TEXT_LIMITS = {"rationale": 2000, "summary": 4000, "reason": 1000}
OBSERVATION_TEXT_LIMIT = 6000
MAX_CONSECUTIVE_INVALID = 3
MAX_IDENTICAL_ACTIONS = 2          # a third identical action on an unchanged workspace is refused
MAX_NO_PROGRESS_REFUSALS = 2

# Gateway / tool error codes → the error class the agent (and the report) sees.
_PERMISSION = {"tool_not_permitted", "path_not_writable", "approval_missing", "plan_changed",
               "connector_not_configured", "invalid_path", "path_escape", "symlink_path"}
_INVALID_REQUEST = {"unknown_tool", "invalid_arguments"}
_MISSING_CONTEXT = {"not_a_file", "no_baseline_copy"}


class DecisionInvalid(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_decision(decision: Any) -> dict:
    """Shape-only validation. Whether a tool is permitted is the gateway's decision, not this one's."""
    if not isinstance(decision, dict):
        raise DecisionInvalid("not_an_object")
    kind = decision.get("kind")
    if kind not in _FIELDS:
        raise DecisionInvalid("unknown_kind")
    allowed, required = _FIELDS[kind]
    extra = set(decision) - allowed
    if extra:
        raise DecisionInvalid("unexpected_fields:" + ",".join(sorted(map(str, extra)))[:200])
    missing = required - set(decision)
    if missing:
        raise DecisionInvalid("missing_fields:" + ",".join(sorted(missing)))
    for name, limit in _TEXT_LIMITS.items():
        if name in decision and (not isinstance(decision[name], str) or len(decision[name]) > limit):
            raise DecisionInvalid(f"invalid_{name}")
    if kind == "tool_call":
        if not isinstance(decision["tool"], str) or not 0 < len(decision["tool"]) <= 64:
            raise DecisionInvalid("invalid_tool")
        if not isinstance(decision["args"], dict):
            raise DecisionInvalid("invalid_args")
    if kind == "stop" and decision["category"] not in STOP_CATEGORIES:
        raise DecisionInvalid("invalid_category")
    if kind == "request_approval":
        for name in ("requested_tools", "requested_paths"):
            value = decision.get(name, [])
            if not isinstance(value, list) or len(value) > 16 or not all(isinstance(v, str) and len(v) <= 240
                                                                          for v in value):
                raise DecisionInvalid(f"invalid_{name}")
    return json.loads(canonical_json(decision))


def classify(outcome: dict) -> str | None:
    """Error taxonomy: None for success, otherwise one class the agent can act on."""
    if outcome.get("ok"):
        result = outcome.get("result") or {}
        if "exit_code" in result:
            if result.get("timed_out"):
                return "execution_error"
            if result.get("exit_code") != 0:
                return "test_failure"
        return None
    error = str(outcome.get("error", ""))
    code = error.split(":", 1)[0]
    if error.startswith("isolation_unavailable"):
        return "execution_unavailable"
    if code == "tool_budget_exhausted":
        return "limit_reached"
    if code == "mission_not_running":
        return "interrupted"
    if code in _PERMISSION:
        return "permission_denied"
    if code in _INVALID_REQUEST:
        return "invalid_request"
    if code in _MISSING_CONTEXT:
        return "missing_context"
    return "tool_error"


_TAIL_KEYS = frozenset({"stdout", "stderr", "output", "test_output_tail"})


def bound(value: Any, limit: int = OBSERVATION_TEXT_LIMIT, _key: str | None = None) -> Any:
    """Truncate long strings: keep the tail of execution output (where failures are reported)
    and the head of everything else (file contents, diffs), and say so."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        if _key in _TAIL_KEYS:
            return f"[truncated: first {len(value) - limit} chars omitted]\n" + value[-limit:]
        return value[:limit] + f"\n[truncated: last {len(value) - limit} chars omitted; read a line range]"
    if isinstance(value, dict):
        return {key: bound(item, limit, key) for key, item in value.items()}
    if isinstance(value, list):
        return [bound(item, limit, _key) for item in value[:200]]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class AgentTurnContext:
    """Everything a provider may see for one decision. Built by trusted code only."""
    mission_id: str
    objective: str
    tools: dict[str, dict]          # permitted tools: name -> {description, effect, input_schema}
    scope: dict                     # writable, protected, limits (from the approved scope)
    iteration: int
    remaining: dict                 # iterations, tool_calls, model_calls, tokens
    history: tuple[dict, ...]       # [{iteration, decision, observation}], oldest first


@dataclass
class DecisionProposal:
    decision: Any
    info: dict = field(default_factory=dict)   # provider, model, simulated, usage


@dataclass
class LoopState:
    iteration: int = 0
    model_calls: int = 0
    tokens: int = 0
    consecutive_invalid: int = 0
    no_progress_refusals: int = 0
    action_counts: dict = field(default_factory=dict)       # (signature) -> times executed
    history: list = field(default_factory=list)
    verified_tree: str | None = None                          # tree sha of the last passing verification


@dataclass
class LoopOutcome:
    verify: bool                     # True: hand over to final runtime verification
    reason: str | None = None        # failure reason when verify is False


class AgentLoop:
    """Runs inside the engine while the mission is ``running``. Persists every turn."""

    def __init__(self, engine, spec, ctx, provider, checks_fn, state: LoopState | None = None):
        self.engine, self.store, self.spec, self.ctx = engine, engine.store, spec, ctx
        self.provider = provider
        self.checks_fn = checks_fn           # () -> (all_passed: bool, verdict: dict); runtime-owned
        self.state = state or LoopState()
        self.budget = spec.budget
        self.tool_specs = {name: {"description": t.description, "effect": t.effect, "input_schema": t.input_schema}
                           for name, t in engine.gateway.registry.items() if name in spec.tools}

    # -- helpers ---------------------------------------------------------------------------
    def _tree(self) -> str:
        return tree_sha256(snapshot(self.ctx.workspace.root))

    def _record(self, kind: str, payload: dict) -> None:
        self.store.append_event(self.spec.mission_id, kind, payload)

    def _context(self) -> AgentTurnContext:
        s = self.state
        remaining = {"iterations": self.budget["max_iterations"] - s.iteration,
                     "tool_calls": self.budget["max_tool_calls"] - self.ctx.tool_calls}
        if not self.provider.simulated:
            remaining["model_calls"] = self.budget["max_model_calls"] - s.model_calls
            if "max_total_tokens" in self.budget:
                remaining["tokens"] = self.budget["max_total_tokens"] - s.tokens
        scope = {"writable": list(self.spec.writable), "protected": list(self.spec.protected),
                 "verification": self.spec.raw["verification"]["command"]}
        return AgentTurnContext(self.spec.mission_id, self.spec.raw["objective"], self.tool_specs, scope,
                                s.iteration, remaining, tuple(s.history))

    def _budget_exhausted(self) -> str | None:
        s = self.state
        if s.iteration >= self.budget["max_iterations"]:
            return "limit:max_iterations"
        if not self.provider.simulated:
            if s.model_calls >= self.budget["max_model_calls"]:
                return "limit:max_model_calls"
            if "max_total_tokens" in self.budget and s.tokens >= self.budget["max_total_tokens"]:
                return "limit:max_total_tokens"
        return None

    # -- one turn ---------------------------------------------------------------------------
    def _observe_tool(self, decision: dict, tree_before: str) -> dict:
        signature = digest([decision["tool"], decision["args"], tree_before])
        seen = self.state.action_counts.get(signature, 0)
        if seen >= MAX_IDENTICAL_ACTIONS:
            self.state.no_progress_refusals += 1
            return {"ok": False, "error_class": "no_progress", "error": "identical_action_on_unchanged_workspace",
                    "detail": f"this exact action already ran {seen} times on this workspace state; "
                              "change approach"}
        self.state.action_counts[signature] = seen + 1
        outcome = self.engine.gateway.call(self.ctx, decision["tool"], decision["args"])
        return {"ok": outcome["ok"] and classify(outcome) is None, "error_class": classify(outcome),
                "error": outcome.get("error"), "result": bound(outcome.get("result"))}

    def run(self) -> LoopOutcome:
        from .mission import MissionState
        s = self.state
        while True:
            if self.store.state(self.spec.mission_id) is not MissionState.RUNNING:
                return LoopOutcome(False, "interrupted")
            exhausted = self._budget_exhausted()
            if exhausted:
                return LoopOutcome(False, exhausted)
            if s.no_progress_refusals >= MAX_NO_PROGRESS_REFUSALS:
                return LoopOutcome(False, "limit:no_progress")
            s.iteration += 1
            try:
                proposal = self.provider.next_decision(self._context())
            except Exception as error:  # ProviderError or an unexpected provider failure: fail closed
                code = getattr(error, "code", type(error).__name__)
                self._record("agent.provider_error", {"iteration": s.iteration, "code": str(code)[:120]})
                return LoopOutcome(False, f"provider_error:{code}")
            usage = (proposal.info or {}).get("usage") or {}
            if not self.provider.simulated:
                s.model_calls += 1
                s.tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            tree_before = self._tree()
            try:
                decision = validate_decision(proposal.decision)
            except DecisionInvalid as invalid:
                s.consecutive_invalid += 1
                observation = {"ok": False, "error_class": "invalid_decision", "error": invalid.code}
                self._finish_turn({"kind": "invalid"}, observation, proposal.info, tree_before)
                if s.consecutive_invalid >= MAX_CONSECUTIVE_INVALID:
                    return LoopOutcome(False, "limit:invalid_decisions")
                continue
            s.consecutive_invalid = 0
            kind = decision["kind"]
            if kind == "tool_call":
                observation = self._observe_tool(decision, tree_before)
            elif kind == "request_verification":
                passed, verdict = self.checks_fn()
                s.verified_tree = tree_before if passed else None
                observation = {"ok": passed, "error_class": None if passed else "verification_failed",
                               "result": bound(verdict)}
            elif kind == "conclude":
                if s.verified_tree is not None and s.verified_tree == tree_before:
                    self._finish_turn(decision, {"ok": True, "error_class": None, "result": "accepted"},
                                      proposal.info, tree_before)
                    return LoopOutcome(True)
                observation = {"ok": False, "error_class": "verification_required",
                               "error": "conclusion_requires_a_passing_runtime_verification_of_the_current_state"}
            elif kind == "request_approval":
                self._finish_turn(decision, {"ok": True, "error_class": None, "result": "handoff_recorded"},
                                  proposal.info, tree_before)
                return LoopOutcome(False, "handoff:approval_requested")
            else:  # stop
                self._finish_turn(decision, {"ok": True, "error_class": None, "result": "stop_recorded"},
                                  proposal.info, tree_before)
                return LoopOutcome(False, f"agent_stopped:{decision['category']}")
            self._finish_turn(decision, observation, proposal.info, tree_before)

    def _finish_turn(self, decision: dict, observation: dict, info: dict | None, tree_before: str) -> None:
        s = self.state
        tree_after = self._tree()
        observation = {**observation, "workspace_changed": tree_after != tree_before}
        turn = {"iteration": s.iteration, "decision": decision, "observation": observation}
        s.history.append(redact(turn))
        provider_info = {k: v for k, v in (info or {}).items() if k in ("provider", "model", "simulated", "usage",
                                                                         "policy", "request_id", "stop_reason")}
        self._record("agent.turn", {**turn, "provider": provider_info})
        self._record("agent.checkpoint", {"iteration": s.iteration, "workspace_tree_sha256": tree_after,
                                          "tool_calls": self.ctx.tool_calls, "model_calls": s.model_calls,
                                          "tokens": s.tokens, "verified_tree": s.verified_tree,
                                          "consecutive_invalid": s.consecutive_invalid,
                                          "no_progress_refusals": s.no_progress_refusals,
                                          "action_counts": s.action_counts})
