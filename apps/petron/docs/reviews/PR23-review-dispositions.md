# PR #23 (PETRON V0.1): disposition of every review thread

Baseline reviewed: `2a3d96e`. Fixes: the commits named below on `fix/petron-v01-security-hardening`.
Each finding was checked against the source before it was adopted; the proposed fixes were not
copied blindly. "Regression test" means a test that failed on the reviewed code and passes now
(the failing output is kept in the hardening evidence, not in the repository).

Dispositions: **Fixed** (with a regression test unless stated), **Already fixed**, **Not applicable**,
**Open** (severity and blocker stated). No thread is marked resolved on GitHub by this change: that is
the reviewers' call after they re-read the new commits.

| # | Thread (file, reviewer, severity) | Disposition | Evidence |
|---|---|---|---|
| 1 | `.github/workflows/petron.yml` CodeRabbit major: mutable action tags | **Fixed** (not regression-testable: CI configuration; verified by inspection) | `77c7443`: checkout v4.4.0 `11d5960a…`, setup-python v5.6.0 `a26af69b…`, resolved from github.com tag refs. Other workflows in the repository still use mutable tags: out of scope here (Open Core workflow controls, see handoff) |
| 2 | `e001-evidence/transcript.txt` approver quoting | **Fixed** | `1b77326`: transcript regenerated from a real run; the argument is quoted as a shell needs it |
| 3 | `README.md` `<hex>` placeholder | **Fixed** | `1b77326`: `YOUR_PLAN_SHA256` |
| 4 | `cli.py` `_load_spec` types | **Fixed** | `e019822`, `MalformedInputTests` (arrays, scalars, non-object `workspace`, non-string `source`, null) |
| 5 | `engine.py` plan() strands mission in `planning` | **Fixed** | `2383247` (catch-all and protected `set_plan`/transition), `PlanningCannotStrandAMission` |
| 6 | `engine.py` run(): copies not compared with the source hash | **Fixed** | `2383247`: both copies are built from bytes hashed against the creation hash; `SourceMatchesApprovalTests` (no sandbox start on a changed source) |
| 7 | `engine.py` two different `diff_sha256` | **Fixed** | `2383247`: the verdict uses the stored artifact's digest; E001 evidence regenerated (`1b77326`) |
| 8 | `redact.py` quoted values with spaces | **Fixed** | `2e43071`, `RedactionShapeTests` (single, double, escaped, multiline, JSON string, unterminated; ordinary prose and token counts untouched; hostile 200 KB input stays bounded) |
| 9 | Codex P1 `connectors/pexisos.py` untrusted MCP server executed on the host | **Fixed, with a stated limit** | `53ede92`: the source tree must be pinned by SHA-256 and only a private read-only copy of the authenticated bytes is started; no pin, no start. `ImpostorTests`: the impostor never writes its marker. **Limit:** pinned code still runs with host privileges (authenticity, not confinement); recorded as residual risk 8 |
| 10 | Codex P1 `anthropic_provider.py` objective sent unredacted | **Fixed** | `2e43071`: an objective with a credential is refused (cannot be redacted without changing the instruction), files redacted, assembled request re-checked; `ProviderRequestTests` with a capturing fake client |
| 11 | Codex P1 `engine.py` validation/read TOCTOU | **Fixed** | `2383247`: `read_tree` through directory descriptors with `O_NOFOLLOW`, device/inode/type verified; `NoFollowReadTests` and `PlanningRaceTests` (15 of 150 racing attempts leaked outside bytes before; 0 now) |
| 12 | Codex P1 `store.py` secrets in `missions.spec_json` | **Fixed** | `2e43071`: rejected before persistence in the engine and again in the store; tests inspect the raw SQLite bytes. Rows written by earlier versions are not rewritten (the spec is immutable and hash-bound); read paths redact them (residual risk 9) |
| 13 | Codex P2 `store.py` truncated event log accepted | **Fixed, with a stated limit** | `e019822`: head anchored beside the database, `events head`, `events verify --expect-head`. **Limit:** the chain is unkeyed; whoever can rewrite the database and the anchor can recompute everything. The independent record is the operator's job (residual risk 2) |
| 14 | Codex P2 `cli.py` E001 fixture missing from the wheel | **Fixed** | `e019822`: fixture shipped as `petron.fixtures`; `InstalledWheelTests` builds and runs a real wheel outside the repository |
| 15 | Codex P1 `sandbox.py` unbounded output | **Fixed** | `fd0b4ff`: continuous drain keeping 48 KiB head + 16 KiB tail; 1.4 GB of host RAM for 384 MiB of output before, bounded now; also proven with a real container (`RealContainerBoundTests`) |
| 16 | Codex P2 `sandbox.py` cleanup when kill fails | **Fixed** | `fd0b4ff`: kill → force-remove → confirmed absent; local client always reaped; `container_cleanup_failed` raised; the container also has its own later deadline |
| 17 | Codex P2 `engine.py` cancel does not stop in-flight execution | **Fixed** | `fd0b4ff`: cancel callback polled by the sandbox; `EngineCancellationTests` |
| 18 | Codex P2 `sandbox.py` exact PID and CPU ceilings | **Fixed** | `fd0b4ff`: `pids.max ≤ 64`, CPU quota measured (cgroup v2 and v1), unreadable fails closed; `ResourceCeilingTests` + a real probe (18 checks) |
| 19 | Codex P2 `anthropic_provider.py` SDK retries | **Fixed** | `2e43071`: `max_retries=0`; `RetryAccountingTests` |
| 20 | Codex P2 `report.py` terminal injection | **Fixed** | `e019822`: control, C1 and bidi/format characters escaped; the diff fence outgrows any backtick run; the stored artifact stays raw; `TerminalSafetyTests` |

Not independently re-reviewed. Green CI is necessary, not sufficient: this branch still needs the
reviewers' own pass and the Open Core / legal gates listed in the handoff.
