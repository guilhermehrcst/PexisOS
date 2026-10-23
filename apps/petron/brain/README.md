# PETRON Brain

The entire PETRON interface is the Brain: one monochrome particle sphere on pure black.
Nothing else is visible: no text, panels, buttons or labels. Status is spoken to screen readers
through a visually hidden live region.

## Two ways to open it

| Mode | How | What it shows |
|---|---|---|
| **Live** (runtime-connected) | `petron brain serve` → open `http://127.0.0.1:8765/` | the real state of PETRON missions in `$PETRON_HOME`, read-only |
| **Visual only** (standalone demo) | open `index.html` directly | the animation; driven only by `window.PetronBrain` calls, never by runtime data |

Live mode exists only behind the bridge: the bridge serves `index.html` with `live.js` added.
The standalone file never connects to anything.

```bash
# from the repository root, with the same PETRON_HOME the CLI uses
PYTHONPATH=apps/petron/src python -m petron brain serve      # or: petron brain serve
```

Then run missions with the normal CLI (`petron experiment e002`, `mission approve`, `mission run`)
and watch the Brain respond.

## States (all monochrome; transitions are interpolated, ~0.6 s)

| Brain state | Shown when (authoritative runtime event) |
|---|---|
| `idle` | no active mission; mission created; approval recorded but not yet running; terminal states older than 15 s |
| `thinking` | `agent.decision_requested` (the runtime is waiting for a provider decision); mission planning |
| `working` | mission running; tool authorized (`tool.authorization` allow); tool result observed; resumed |
| `verifying` | `agent.verification_started`, mission `verifying`, `verification.result` |
| `awaiting_approval` | mission `awaiting_approval` (nothing is approved until the CLI records it) |
| `success` | mission `completed`, only if the transition is fresher than 15 s; held 4.5 s, then idle |
| `error` | mission `failed` (fresh), tool denied, provider error, execution disabled |
| `disconnected` | the bridge stream is lost (after a 1.5 s grace); never shown as activity |
| `listening` | reserved: only while a real input-capture session is active (none exists yet) |

`activity` is a fixed heuristic per event type (e.g. executing a tool animates more than reading
a file). It is not a measurement: no CPU, token or progress figure is invented.

A running mission whose runner process has died (its lease is dead) is shown as calm `idle`, not
as work. Reconnection replays missed events (old completions map to `idle`) and then applies the
authoritative snapshot, so an old success is never celebrated again.

## Bridge security (`apps/petron/src/petron/brain_bridge.py`)

Loopback bind only; fixed asset allowlist (no URL path reaches the filesystem); `Host` must name
the loopback server (DNS-rebinding guard); a present `Origin` must be this server; cross-site
fetches refused; no CORS headers; GET/HEAD only (no mutation endpoint exists); strict CSP
(`default-src 'none'`, self-only scripts); SQLite opened read-only; event payload contents are
never forwarded.

## Interaction

Pointer/touch: parallax and a discreet pulse. Keyboard: the Brain is focusable (Tab); Enter or
Space gives the same pulse and re-announces the status. **No interaction starts, approves or
changes a mission.**

## Voice: interface contract (not implemented in this increment)

Not shipped, on purpose: browser speech recognition is not reliably local (Chromium's Web
Speech API sends audio to a remote service), and creating missions from the browser needs an
authenticated command channel that does not exist yet. The contract for the next increment:

1. **Capture** only after an explicit gesture on the Brain and the browser's microphone
   permission; the Brain shows `listening` only while capture is actually running.
2. **Transcription** by a local engine by default; any remote engine needs explicit opt-in.
3. **Draft, never execute**: the transcript becomes a mission *draft* via a future
   `POST /api/drafts`, protected by a per-run secret printed in the operator's terminal (never
   embedded in static files), same-origin checks and a strict schema. A draft is a `created`
   mission; approval stays a separate operator act bound to the scope hash (`mission approve`).
4. **No paid model** sees a transcript unless the operator runs the mission with
   `--allow-real-model`.

## Notes on the approved visual

The prototype's renderer is preserved. Additions: four states, interpolated parameters, an
integrated animation phase (state changes no longer jump the motion), the texture layer
following state luminosity (idle unchanged at 0.92), `pulse()` and `modes`. The approved
palette contains very faint cool tints (halo `rgb(150,160,185)` at ≤2.3 % alpha, particles
`rgb(241,246,255)`); they are kept as approved (measured chroma ≤ 0.36/255 in screenshots).

## Tests

- `apps/petron/tests/test_brain_bridge.py`: mapping, freshness, snapshots, reconnection,
  server refusals (part of the normal suite).
- `apps/petron/tests/e2e/brain_e2e.py`: real browser, real bridge, real E002 + failing mission
  + bridge kill/restart (needs Playwright, Chromium and Docker).
