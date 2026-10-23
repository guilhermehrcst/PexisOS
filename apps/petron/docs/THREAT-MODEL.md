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
| Source tree modified | Mission works on a copy built from bytes whose hash equals the one recorded at creation; a changed source is refused before the provider call and before any sandbox start | Source tree hash at creation vs at planning, at copy, and at verification | `workspace:source_changed` / `verification_failed:source_tree_unchanged` |
| Path traversal / symlink escape | Relative normalized paths only; symlinks rejected in the source tree and on every path hop; every read is done through directory descriptors with `O_NOFOLLOW` and the opened object's device/inode/type are checked | The tree hash is computed from the same bytes that are copied and sent, and compared with the hash recorded at creation | `symlink_in_tree` / `tree_changed_during_read` / `source_changed`; nothing is sent or run |
| Double run / race | Compare-and-set transitions | SQL trigger rejects illegal transitions | One run wins; the other gets `state_conflict` |
| Evidence tampering | Triggers forbid UPDATE/DELETE on events, approvals, artifacts | Unkeyed SHA-256 hash chain (`petron events verify`) detects edits and middle deletions even when the triggers are dropped; the head is anchored beside the database and can be recorded elsewhere (`events head`, `--expect-head`) to detect truncation of the newest events or a rollback | Report shows chain BROKEN. **Not tamper-proof** against whoever can rewrite the database and the anchor: see residual risk 2 |
| Secrets in the database, reports or provider requests | A spec, plan or approval holding a recognizable credential is rejected before it is stored (the objective cannot be redacted without changing the instruction); events and artifacts are redacted before every write; file contents are redacted and the assembled provider request is re-checked; reports redact on the way out | `only_expected_env` probe check; tests inspect the raw SQLite bytes | `secret_in_spec` / `secret_in_request`; value replaced by `[REDACTED]` |
| Impostor / write-capable Fabric | The MCP server source tree must be pinned by SHA-256 by the operator and is started only from a private read-only copy of the authenticated bytes; no pin, no start. Also `-I -B`, minimal env, empty cwd; only five tool names callable | Server name, exact tool set and read-only annotations checked after `initialize` | `source_not_pinned` / `source_hash_mismatch` before any process exists; connector refuses to work. **Authenticity, not confinement:** pinned code still runs with host privileges (residual risk 8) |
| Sandbox output flood / stuck container | Output is drained continuously and only the first 48 KiB and last 16 KiB per stream are kept (host memory is constant); the container has its own later deadline; cancel and timeout kill it | Cleanup is confirmed (`kill`, then `rm -f`, then a listing); the probe measures pids/CPU/memory ceilings exactly | `container_cleanup_failed` is raised, never swallowed |
| Terminal injection through reports | Untrusted text (diff, objective, reasons) is rendered with control, C1 and bidi characters escaped; the diff fence outgrows any backtick run | The stored artifact keeps the raw text | Visible escapes instead of effects |
| Real model used without consent or credit | Needs `--allow-real-model`, `ANTHROPIC_API_KEY`, and a model-call budget; explicit `base_url` | `provider.call` events counted against the budget | `provider_error:*` |

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
2. **The hash chain is unkeyed.** Someone with write access to the database file *and* the local
   anchor can rewrite the whole chain consistently. The only real defence is an independent record
   of the head (`petron events head`, kept outside this host or under a different account) checked
   with `--expect-head`; PETRON does not do that for you. Signing is future work.
3. **Approver identity is a string.** V0.1 records who approved, it does not authenticate them.
4. **Redaction is pattern-based.** It reduces, not eliminates, leakage.
5. **Prompt injection is not prevented.** It is contained: an injected plan still needs human
   approval and still runs only through the gateway and the sandbox.
6. **Denial of service inside limits.** A test can consume its CPU/memory/time budget.
7. **PexisOS limits carried over:** frozen demo clock (FAB-01), in-memory audit log (FAB-03).
8. **MCP server code is authenticated, not confined.** Once the operator pins a source tree, that
   code runs with the host user's privileges. Confining it needs a container image that carries
   the MCP SDK; not implemented. The pin must come from a reviewed source, not from a digest
   computed on the spot.
9. **Plaintext written by earlier versions.** Specs stored before this hardening may contain
   credentials; read paths redact them but the rows are not rewritten (the spec is immutable and
   hash-bound). Operators should treat such databases as sensitive.
