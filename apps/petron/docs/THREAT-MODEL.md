# PETRON V0.1 — Threat model

Scope: a single operator on a workstation, running PETRON from the CLI on local source trees,
with an optional real model provider and an optional read-only PexisOS connector.

## Assets

- the operator's host (files, credentials, network position, Docker daemon);
- source trees the mission reads (must never be modified in place);
- the evidence record (event log, approvals, diffs, reports);
- PexisOS and any physical system it may observe (must never be actuated).

## Adversaries and failure sources

| Source | Examples |
|---|---|
| Model output | wrong plan, prompt-injected plan (instructions hidden in workspace files), attempts to name its own permissions or principal |
| Workspace content | malicious tests or code that runs during verification; symlinks pointing outside |
| Hostile or impostor MCP server | advertises write tools, lies about read-only status, impersonates the Fabric |
| Concurrency and operator error | two runs of one mission, stale approval, a cancel during a run |
| Local tampering | direct SQL edits of the evidence database |
| Secret leakage | keys in files, environment, tool outputs or logs |

## Hazards → barriers (defense in depth)

| Hazard | Prevention | Detection / containment | Safe state |
|---|---|---|---|
| Model grants itself permissions | Permissions live only in the operator spec; every schema closed; provider output is a plan, not a call | Plan validation (`plan_rejected`) **and** gateway re-check per call | Mission `failed`, nothing executed |
| Plan runs without consent | Approval bound to the plan sha256; plan immutable (SQL trigger) | Gateway re-checks approval and plan hash before any `execute` | Run refused, state unchanged |
| Code escapes onto the host | Code runs only in the container: no network, `--cap-drop ALL`, `no-new-privileges`, seccomp, read-only root and workspace, uid 65534, pids/memory/CPU limits, no Docker socket, no host home, `--pull never` | The probe measures every property from inside before enabling execution | Execution disabled; mission fails closed, never falls back to the host |
| Tests weakened to get green | Tests are protected paths; gateway refuses writes outside `writable` | Verification compares protected-path hashes; container mounts the workspace read-only | `verification_failed` |
| Source tree modified | Mission works on a copy | Source tree hash at creation vs at verification | `verification_failed:source_tree_unchanged` |
| Path traversal / symlink escape | Relative normalized paths only; symlinks rejected in the source tree and on every path hop | — | `step_failed:…:invalid_path` / `symlink_path` |
| Double run / race | Compare-and-set transitions | SQL trigger rejects illegal transitions | One run wins; the other gets `state_conflict` |
| Evidence tampering | Triggers forbid UPDATE/DELETE on events, approvals, artifacts | SHA-256 hash chain (`petron events verify`) detects edits and deletions even when the triggers are dropped | Report shows chain BROKEN |
| Secrets in logs | Secret redaction before every write; providers see redacted snapshots; container gets only the image env | `only_expected_env` probe check | Value replaced by `[REDACTED]` |
| Impostor / write-capable Fabric | Connector launches with `-I -B`, minimal env, empty cwd; only five tool names callable | Server name, exact tool set and read-only annotations checked after `initialize` | Connector refuses to work |
| Real model used without consent or credit | Needs `--allow-real-model`, `ANTHROPIC_API_KEY`, and a model-call budget; explicit `base_url` | `provider.call` events counted against the budget | `provider_error:*` |

### V0.2 agent loop

| Hazard | Prevention | Detection / containment | Safe state |
|---|---|---|---|
| Model claims success | `conclude` accepted only after a passing runtime verification on the same workspace state | final V0.1 verification runs again | `verification_required` observation; mission cannot complete |
| Model fabricates tool results or grants itself permissions | closed decision schema (no result/permission fields) | invalid decisions counted | `limit:invalid_decisions` |
| Endless or repetitive loop | iteration, tool-call, model-call and token limits | any identical decision on an unchanged workspace refused after two runs | `limit:*` |
| Widened scope after approval | scope stored write-once, approval bound to its hash | scope recomputed from the spec at run time | `approval_missing` / `plan_integrity` |
| Two runners on one mission | compare-and-set transitions | run lease (pid/host) | `lease_held` |
| Resume repeats an unknown effect | resume only from a checkpoint | workspace tree must equal the checkpoint's | `resume_refused:workspace_diverged` |
| Silent model substitution | plain Messages API, no fallback option | refusal recorded | `provider_error:model_refusal` |

### Independence of barriers

- Plan validation and gateway authorization share one premise (the spec's tool list), so they
  are **not** independent against a wrong spec. The independent barriers behind them are
  workspace confinement (path rules) and the container (kernel isolation).
- The SQL triggers and the hash chain are independent: dropping the triggers does not defeat
  the chain, and the chain does not depend on the triggers.
- The connector's tool-set check and the Fabric's own closed schemas are independent: a
  spoofed `principal` argument is rejected by both (tested).

## Residual risks (accepted for V0.1, owner to review)

1. **Rootful Docker daemon.** The container is unprivileged, but a kernel or runc escape
   would land as root on the host. Rootless Docker (or gVisor) is the next step. The operator
   who can start missions can also use Docker directly; PETRON adds no new privilege.
2. **The hash chain is unkeyed.** Someone with write access to the database file can rewrite
   the whole chain consistently. Anchoring the head hash externally (or signing) is future work.
3. **Approver identity is a string.** V0.1 records who approved, it does not authenticate them.
4. **Redaction is pattern-based.** It reduces, not eliminates, leakage.
5. **Prompt injection is not prevented.** It is contained: an injected plan still needs human
   approval and still runs only through the gateway and the sandbox.
6. **Denial of service inside limits.** A test can consume its CPU/memory/time budget.
7. **PexisOS limits carried over:** frozen demo clock (FAB-01), in-memory audit log (FAB-03).
