# PR #24 (PETRON V0.2): disposition of every review thread

Baseline reviewed: `6fcc54d`. This branch contains the V0.1 hardening (see
`PR23-review-dispositions.md`; merged as `8b7f3ee`) plus the commits named below. Findings were
verified against the source before adoption. "Regression test" means it failed on the reviewed code
and passes now.

Dispositions: **Fixed**, **Already fixed**, **Not applicable**, **Open**. No thread is marked
resolved on GitHub by this change.

| # | Thread (file, reviewer, severity) | Disposition | Evidence |
|---|---|---|---|
| 1 | `e002-evidence/transcript.txt` approver quoting | **Fixed** | `8f30450`: E002-A evidence regenerated from a real run |
| 2 | `PETRON-E002.md` `<hex>` in the E002-B approval | **Fixed** | `8f30450`: hash held in a shell variable |
| 3 | `README.md` blocking commands in one terminal | **Fixed** | `8f30450`: labelled for separate terminals; `stop` shown while the mission runs |
| 4 | `engine.py` `_run_agent` strands the mission in `verifying` when `_verify` raises | **Fixed** | `6b96296`: failure recorded from `VERIFYING`; `test_an_unexpected_error_in_final_verification_fails_the_mission_instead_of_stranding_it` |
| 5 | `report.py` multiline rationale breaks the turns table | **Fixed** | `8b7f3ee` + `6b96296`: every cell is one escaped line; `test_hostile_rationales_cannot_forge_rows_or_inject_escapes_in_the_report` |
| 6 | `tools.py` `summarize_unittest` raises / first match wins | **Fixed** | `6b96296`: total over any input, last summary wins, counts clamped; `UntrustedOutputTests` |
| 7 | Codex P1 `providers/__init__.py` defaults resolved after approval | **Fixed** | `2801c11`: settings resolved into the spec before the scope hash; factories refuse an unresolved block; the provider about to run must equal the approved scope (`provider_settings_changed`); `ProviderSettingsBindingTests` (changed defaults cannot alter the approved terms; a pre-hardening row fails `spec_integrity`) |
| 8 | Codex P1 `mission.py` no aggregate token cap | **Fixed** | `2801c11`: a real-provider agent mission without `max_total_tokens` is refused at parse; `AggregateTokenBudgetTests` |
| 9 | Codex P1 `agent.py` cap enforced only after the spend | **Fixed, with a stated property** | `2801c11` + `6b96296`: before each request the output allowance is reserved against a hard upper bound of the input (UTF-8 bytes; a token covers at least one byte) and `max_tokens` is capped to what remains; a request that cannot fit is refused without any HTTP call; `TokenCapBeforeCallTests`. **Property:** the bound is deliberately pessimistic (several times the real token count), so the cap is a guarantee, not a forecast; operators should size `max_total_tokens` accordingly |
| 10 | Codex P1 `agent.py` `request_verification` after a cancel | **Fixed** | `6b96296`: the authoritative state is rechecked after every provider answer and before verification/conclusion; `_checks` refuses a stopped mission; the late answer is recorded as discarded; `CancellationTests` (nothing starts in the work copy) |
| 11 | Codex P2 `engine.py` resume restores a turn without its checkpoint | **Fixed** (by refusing) | `6b96296`: `resume_refused:uncheckpointed_turn`; the tail cannot be reconstructed without guessing, so it is not restored; `test_a_turn_after_the_last_checkpoint_is_not_restored` |
| 12 | Codex P2 `anthropic_provider.py` usage lost on refusal/truncation | **Fixed** | `2801c11` + `6b96296`: usage travels on the `ProviderError`, counted in the loop, recorded in `agent.provider_error`, shown in the report; `test_usage_of_a_refused_or_truncated_response_is_not_lost` |
| 13 | Codex P1 `store.py` same-PID concurrent runners | **Fixed** | `6b96296`: lease identity is a per-run token; atomic check-and-insert; in-process registry; reused pids detected by start time; superseded runner stops without failing the new owner; `LeaseTests` (30 racing rounds, concurrent resumes run the mission once) |
| 14 | Codex P2 `engine.py` resume without verifying the event chain | **Fixed** | `6b96296`: `resume_refused:event_chain_broken`; test |
| 15 | Codex P2 `engine.py` baseline tree not validated on resume | **Fixed** | `6b96296`: `resume_refused:baseline_diverged`; test |
| 16 | Codex P2 `engine.py` crash leaves `verifying` unrecoverable | **Fixed** | `6b96296`: `resume` finishes a crashed verification idempotently (artifacts reused or kept side by side; an existing verdict is honored); `test_a_crash_during_final_verification_is_recoverable`, `test_recovery_after_a_partially_written_verification_does_not_collide` |
| 17 | Codex P1 `engine.py` changed source reaches the agent loop | **Fixed** | `2383247` (inherited via `8b7f3ee`): both copies are built from bytes hashed against the creation hash; V0.2 regression `SourceIntegrityBeforeTheLoopTests` asserts zero provider calls and no sandbox run |
| 18 | Codex P1 `anthropic_provider.py` agent client SDK retries | **Fixed** | `2e43071` (shared client builder, `max_retries=0`); `NoHiddenRetryTests`: one decision is one HTTP attempt and is counted, also when it fails |
| 19 | Codex P2 `tools.py` capped output loses the unittest summary | **Fixed** | `fd0b4ff` (head + tail capture; exit code unaffected) + `test_a_capped_output_still_yields_exit_code_and_summary` |
| 20 | Codex P2 `report.py` model rationales rendered unescaped | **Fixed** | same as row 5 |

## What this branch still does not do

- E002-B (a real model) has **not** been run: no API key, no authorization for paid calls. The
  provider is tested only against fake clients.
- Resume remains unsupported for a real provider (its conversation lives in memory).
- The MCP server is authenticated by a pinned hash, not confined (inherited limit, residual risk 8).
- Not independently re-reviewed. Green CI is necessary, not sufficient.
