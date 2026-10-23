# PETRON V0.1 — Architecture

```
operator ──spec.json──▶ CLI ──▶ Engine ─────────────────────────────────────────┐
                                  │                                              │
                 ┌────────────────┼───────────────┬──────────────┬────────────┐ │
                 ▼                ▼               ▼              ▼            ▼ ▼
           Model Runtime     Tool Gateway    Execution       PexisOS      Memory Engine
           (providers)       (registry +     Runtime         Connector    (SQLite store:
           deterministic |   authorization)  (Docker         (MCP stdio,   missions, events,
           anthropic opt.        │           sandbox,         read-only)   approvals,
                 │               │           probed)              │        artifacts)
                 └── plan ──▶ validate ── hash ── approval ── steps ──▶ verify ──▶ report
                                                                             (Observability)
```

| Module | File(s) | Responsibility |
|---|---|---|
| Core | `mission.py`, `schema.py`, `engine.py` | closed spec, state machine, plan contract, orchestration, runtime-owned verification |
| Model Runtime | `providers/` | `Provider.propose_plan()`; deterministic scripts; optional Anthropic Messages API |
| Memory Engine | `store.py` | SQLite: CAS transitions, SQL triggers, append-only hash-chained events, approvals, artifacts |
| Tool Gateway | `tools.py` | registry with closed schemas and effect classes; per-call authorization and evidence |
| Execution Runtime | `sandbox.py`, `workspace.py` | disposable workspace; Docker isolation measured by a probe; no host fallback |
| PexisOS Connector | `connectors/pexisos.py` | read-only MCP client with server identity and tool-set checks |
| Observability | `report.py`, `redact.py` | reports built only from persisted evidence; secret redaction |

## Mission lifecycle

1. `create` — spec validated (closed schema + semantic checks); source tree hashed; `mission.created`.
2. `plan` — `created → planning`; provider receives the permitted tools and a bounded, redacted
   snapshot; plan validated against registry + spec; plan stored once with its sha256;
   `planning → awaiting_approval`. Rejection → `failed`.
3. `approve` — operator records approver + channel for that exact plan sha256.
4. `run` — CAS `awaiting_approval → running`; sandbox probed; base + work copies created;
   baseline tests in the sandbox (defect reproduced?); each step through the gateway; first
   failure → `failed`.
5. `verify` — `running → verifying`; runtime computes: changes within writable paths,
   protected paths unchanged, source unchanged, tests pass in the sandbox, event chain intact.
   All true → `completed`, else `failed:verification_failed:<checks>`.
6. `report` — Markdown/JSON from the database only.

## Data

`$PETRON_HOME/petron.sqlite3` (mode 0600, directory 0700) and `$PETRON_HOME/workspaces/<id>/{base,work}`
(removable with `petron mission discard`). No other state.

## Dependencies

Runtime: Python ≥ 3.11 standard library. Optional: `anthropic>=2.1,<3` (real provider),
`mcp==2.2.0` (connector, same pin as PexisOS). Execution needs a Docker daemon and the
pinned image present locally (`--pull never`).
