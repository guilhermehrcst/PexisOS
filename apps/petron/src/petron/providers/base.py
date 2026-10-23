"""Provider contract: a provider *proposes* a plan; it never executes anything.

A provider receives only what trusted code chose to show it (objective, the tools the mission
permits, a bounded read-only snapshot of the workspace) and returns a plan in the closed
``petron.plan.v0`` shape. The runtime validates, hashes and gates that plan; nothing a provider
returns can alter the mission spec, its permissions or its budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlanRequest:
    mission_id: str
    objective: str
    tools: dict[str, dict]            # name -> {"description", "effect", "input_schema"} (permitted only)
    files: dict[str, str]             # read-only snapshot: path -> text (bounded, redacted)
    writable: tuple[str, ...]
    protected: tuple[str, ...]
    max_steps: int


@dataclass
class PlanProposal:
    plan: dict
    info: dict = field(default_factory=dict)   # provider, model, simulated, usage


class Provider(Protocol):
    kind: str
    simulated: bool

    def propose_plan(self, request: PlanRequest) -> PlanProposal: ...
