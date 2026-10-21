# ADR-0001 — PETRON V0.1 architecture

- Status: accepted for V0.1 (owner review pending)
- Date: 2026-10-21

## Context

PETRON is an agent runtime for software engineering and supervised automation. It must be
useful on a CPU-only 8 GiB workstation, work offline with a deterministic provider, and never
let a model decide what it is allowed to do. It is independent of PexisOS: the only
connection is an optional, versioned, read-only client of the PexisOS Agent Fabric.

## Decision

1. **One Python package, standard library only.** `dependencies = []`. Optional extras:
   `anthropic` (real provider) and `mcp==2.2.0` (PexisOS connector). No services, queues,
   vector stores, Redis, Kubernetes, or frontends.
2. **CLI first** (`petron`), state in **one SQLite file** under `$PETRON_HOME`.
3. **Mission = closed, operator-supplied spec** (`petron.mission.v0`). The spec holds identity,
   objective, workspace, writable and protected paths, tool permissions, verification command
   key, budget, provider and sandbox. It is trusted input written by the operator; nothing a
   provider returns can change it.
4. **Explicit state machine**:
   `created → planning → awaiting_approval → running → verifying → completed | failed`,
   plus `cancelled` from any non-terminal state and `failed` from any active state. Transitions
   are compare-and-set in SQL (`UPDATE … WHERE state = ?`) and a SQLite trigger rejects any
   transition outside the table, even from direct SQL.
5. **Provider proposes, runtime disposes.** A provider returns a *plan* (closed schema: steps of
   `{tool, args}` plus a rationale). The runtime validates it, hashes it, and requires a
   recorded approval bound to that hash before running. A changed plan invalidates the approval.
6. **Tool Gateway** is the only path from a plan to an effect. Each call is checked against the
   tool registry (closed input schema), the mission permissions, the tool's effect class
   (`read`, `write_workspace`, `execute`, `external_read`), the budget, and the workspace
   confinement rules. Every decision is an event.
7. **Disposable workspace + isolated execution.** The mission works on a fresh copy of the
   source tree. Code runs only inside a Docker container with no network, no capabilities,
   `no-new-privileges`, read-only root, read-only workspace mount, unprivileged uid, pids,
   memory and CPU limits, no Docker socket and no host home. The command comes from a fixed
   table selected by the spec; the model cannot supply argv. The sandbox is probed (measured)
   before use; on any failed check execution is disabled and the mission fails closed.
8. **Verification is owned by the runtime**, never by the provider: tests run in the sandbox
   (before the change, to prove the defect, and after), protected paths must be byte-identical,
   the diff may touch only writable paths, and the source tree must be unchanged.
9. **Append-only, hash-chained event log** in SQLite (triggers forbid `UPDATE`/`DELETE`);
   every persisted string goes through secret redaction first.
10. **PexisOS connector (M3)** is an MCP stdio client that launches
    `python -P -m pexislink fabric mcp-stdio --principal <operator-chosen>` with a minimal
    environment, accepts only the five known read-only tools, and refuses any other tool the
    server might advertise. It is never imported by PexisOS and adds nothing to PexisOS.

## Consequences

- No AI dependency enters PexisOS; PexisOS keeps working offline unchanged.
- With the deterministic provider PETRON demonstrates infrastructure, not autonomy.
- The real provider is optional, needs an explicit API key, an explicit flag and a call budget,
  and is not exercised without the owner's authorization.
- Docker rootful is the only isolation backend measured here; on machines without a working,
  probed backend, PETRON can plan and edit but cannot execute.

## Alternatives rejected

- Running tests on the host in a temp dir: a temp dir is not a sandbox.
- Letting the model choose shell commands: violates "model never defines its permissions".
- Embedding PETRON in the Agent Fabric package: forbidden by the boundary.
- A multi-agent / distributed design: unjustified for V0.1.
