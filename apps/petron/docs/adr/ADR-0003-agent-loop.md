# ADR-0003 — Iterative agent loop with scope-bound authorization (V0.2)

- Status: accepted for V0.2 (owner review pending)
- Date: 2026-10-22

## Context

V0.1 runs a static plan that the operator approves by its hash. A useful engineering agent must
choose its next action after seeing the previous result, which a plan approved in advance cannot
express. The separation of model and authority, the gateway, the sandbox and runtime-owned
verification must survive the change.

## Decision

1. **New mission mode, old one untouched.** `mode: "agent"` in the spec selects the loop;
   absent means `plan` (V0.1), so V0.1 specs, hashes and behaviour do not change. Plan mode keeps
   its V0.1 bound of 8 model calls.
2. **Approve the scope, not the steps.** `scope_document(spec)` lists everything the loop may
   use: tools, writable and protected paths, verification command, every budget, provider and
   sandbox. It is derived only from the immutable spec, stored in the same write-once column as
   V0.1 plans (same SQL trigger), and approved by the same hash-bound approval. At run time the
   stored scope must equal the scope recomputed from the spec, or the mission fails closed.
3. **One closed decision per turn** (`petron.decision.v0`): `tool_call`, `request_verification`,
   `conclude`, `request_approval`, `stop`. Unknown fields are rejected, so a decision cannot carry
   results, permissions or identities. Whether a tool is allowed stays the gateway's decision.
4. **Observations come from trusted code**, with an error taxonomy (`test_failure`,
   `execution_error`, `execution_unavailable`, `permission_denied`, `invalid_request`,
   `missing_context`, `tool_error`, `limit_reached`, `no_progress`, `verification_required`,
   `verification_failed`, `invalid_decision`).
5. **Success is the runtime's call.** `conclude` is accepted only right after a passing
   `request_verification` on the unchanged workspace; then the full V0.1 verification runs again.
6. **Bounded by construction:** iterations, tool calls, model calls and tokens; three invalid
   decisions in a row; identical actions on an unchanged workspace are refused after two runs.
7. **Persistence and recovery:** every turn (`agent.turn`) and checkpoint (`agent.checkpoint`)
   is an event in the hash-chained log. A run lease (`run.lease`) blocks a second live runner.
   `resume` continues from the last checkpoint only if the workspace tree matches it exactly;
   otherwise the effect of the interrupted step is unknown and the mission fails closed.
   Resume is supported for deterministic providers only: a real model's conversation lives in
   memory in V0.2.
8. **Providers stay outside the engine.** `next_decision(ctx) -> DecisionProposal`. The Claude
   provider keeps an append-only conversation, returns observations as `tool_result`, asks for one
   tool per turn, and uses the plain Messages API without server-side fallback: a refusal is
   recorded as `provider_error:model_refusal`, never answered by another model.

## Equivalence of controls with V0.1

| Control | V0.1 (plan) | V0.2 (agent) |
|---|---|---|
| Human approval before any execution | plan hash | scope hash, same table and check |
| Approved document immutable | SQL trigger | same trigger, plus run-time recomputation |
| Per-call authorization | gateway | same gateway, same effect classes |
| Writes limited to writable, non-protected paths | gateway + verification | same |
| Code runs only in the probed sandbox | yes | yes |
| Success decided by runtime verification | yes | yes, and `conclude` needs a prior passing check |
| Budget | steps, tool calls | iterations, tool calls, model calls, tokens |

What widens: within the approved scope the agent chooses the order and content of actions
(for example which text to replace in a writable file) without a per-step approval. That is the
purpose of the mode, and it is bounded by the scope, the gateway and the limits above.

## Consequences

- E002-A demonstrates the loop with a deterministic policy; it is not evidence of reasoning.
- E002-B (Claude) is prepared but needs an API key, `--allow-real-model` and a budget.
- Approver identity is still an unauthenticated string; the hash chain is still unkeyed.
