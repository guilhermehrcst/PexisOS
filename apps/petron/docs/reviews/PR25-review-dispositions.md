# PR #25 review dispositions (PETRON Brain, colored edition integrated with V0.2)

Scope: the six CodeRabbit findings and the five Codex findings open on PR #25 at head `509b92c`.
Every fix was reproduced by a failing test first (the evidence is in the commit messages and in
`tests/test_brain_hardening.py`, `tests/test_brain_live_js.py`, `tests/js/live_draft_harness.js`,
`tests/e2e/brain_interaction_e2e.py`). No thread is marked resolved by this work: that happens only
after the fixes are published and verified. No push, merge or paid call was made.

## CodeRabbit

| # | Finding | Disposition | Evidence |
|---|---|---|---|
| 1 | `live.js`: a stale draft blocks new missions until a reload | **Fixed** | The snapshot carries the one pending/running draft of *this bridge*; the page reconciles on every (re)connection (forgets, keeps or recovers). `not_awaiting_approval`, `unknown_mission` and `not_a_brain_draft` drop the draft; other refusals keep it. Runtime events never create a draft. Node harness: the previous `live.js` passes 7 of 15 scenarios, this one 15/15. Real browser: external cancel while connected, and while the stream is down (TCP proxy), each followed by a new mission. |
| 2 | `results.json` does not match the colored page | **Fixed by regeneration** | Both browser suites were re-run on the committed code (see "Browser evidence"); the files record the commit under test and whether the tree had edits. |
| 3 | `brain_bridge.py`: a runner that dies while `verifying` is shown as verifying forever | **Fixed** | `running` and `verifying` use the same V0.2 lease rules: dead holder, or a released lease on a non-terminal mission, means *interrupted* (calm `idle`, "missão interrompida; retome pela CLI", no success, no activity). Heartbeat `status` and the reconnect snapshot carry it. Tests: `InterruptedRunnerTests`, `StreamTests`. |
| 4 | `brain_bridge.py`: a refused POST leaves its body on a reusable HTTP/1.1 connection | **Fixed** | Every refusal sets `Connection: close` and ends the connection. Raw-socket tests pipeline a second request inside the body of nine kinds of refusal (wrong/missing key, bad/missing origin, bad host, wrong content type, unknown path, oversized, short length), a read-only POST and a chunked POST; exactly one response is produced each time. A stalled client is dropped after 30 s; an unexpected failure answers a controlled `500 internal_error`. Origin, Host, key, loopback bind and the absence of CORS are unchanged. |
| 5 | `brain_commands.py`: a failure after `engine.create` leaves an active orphan draft | **Fixed** | Setup failures (brain tag, planning, even `create` after persisting) cancel the mission with reason `draft_setup_failed` and a `brain.draft_failed` event (class name only), and answer `500 draft_failed`. If the cancellation itself cannot be stored the answer is `503 draft_failed_not_cancelled` and nothing claims otherwise. Faults are injected into `append_event`, `set_scope` and the cancelling transition; a new draft works after a recoverable failure. |
| 6 | `brain_commands.py`: the runtime refuses the run after approval | **Fixed** | The mission ends `failed` with reason `run_refused:<code>` and a `brain.run_refused` event (code and scope hash); the approval record is kept and cannot be reused (`409 not_awaiting_approval`); the approve answer is `approved`, not `running`; the snapshot never restores it as pending. An operator cancel that wins the race is not overwritten. A fake provider factory refuses before any client exists (no paid call). Real browser: the Brain goes to `error`, never to `working`/`verifying`/`success`, and settles to calm. |

## Codex

| Finding | Disposition | Evidence |
|---|---|---|
| **P1** bind each draft to the bridge that may approve it | **Fixed** | Each bridge run has an identity recorded with every draft it creates (`brain.draft` also records the template hash). Approve and discard require it (`403 not_a_brain_draft` otherwise), the active-draft check counts only the bridge's own drafts, and a snapshot reports only the bridge's own draft; the read-only bridge exposes none. `BridgeOwnershipTests` (two bridges, same home, different templates; drafts of earlier runs without an identity). Consequence: after restarting the bridge, a pending draft of the earlier run is no longer approvable from the browser; it is cancelled with the CLI. |
| P2 `localhost` may resolve to a non-loopback address | **Fixed** | `localhost` is accepted as a name, but the loopback literal is bound; the resolver never sees it. Test: `LoopbackBindTests`. |
| P2 setup failures are swallowed (unavailable provider) | **Fixed** | Same change as CodeRabbit 6 (including a non-`EngineError` raised before the run starts: `internal_error:<ClassName>`). |
| P2 live failure never returns to idle | **Fixed** | A terminal failure settles to calm idle after 8 s; other errors (a denied action) do not use a timer. Harness scenarios. |
| P2 an agent killed before taking a lease is shown as active forever | **Fixed, bounded** | An *agent* mission with no lease at all is judged interrupted only after its start-up window (its `exec_timeout_s` plus 120 s, measured from its last transition). Plan-mode missions never take a lease and are not judged. `InterruptedRunnerTests`. |

One defect was found by the new browser scenarios rather than by a reviewer: the click a browser fires
when a hold gesture ends opened an unrequested typing session after a quickly failing run. The release
click is now ignored (harness scenario, red before the fix).

## Changes of contract that existing callers may notice

- `POST …/approve` answers `"state": "approved"` (was `"running"`): approval is not execution.
- The snapshot's `brain_draft`/`scope_sha256` fields are replaced by `draft`
  (`{mission_id, state, approved, scope_sha256?}` or null); only the bridge's own draft appears in it.
- A draft created by an earlier bridge run, or by another bridge, can no longer be approved or discarded
  through this bridge.

## Limits that remain (stated, not hidden)

- The per-run key is still the only credential; the hold gesture proves intent, not identity.
- If the store is unavailable at the moment a refusal or a failed setup must be recorded, nothing is
  claimed: the mission is left as it is for the operator (`petron mission stop`).
- The interrupted-runner decision is a liveness heuristic over leases and a start-up window; it can
  report a slow start-up as interrupted only after that window, and cannot see a runner on another host.
- The WebP textures remain under pending intellectual-property review; no code change approves them.
- Independent human review and the Open Core / legal gates are not closed by this document.
