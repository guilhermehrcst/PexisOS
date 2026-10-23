# PETRON Brain

The entire PETRON interface is the Brain: one particle sphere on pure black, in color by default.
Nothing else is visible: no text, panels, buttons or labels. Status is spoken to screen readers
through a visually hidden live region.

## Two ways to open it

| Mode | How | What it shows |
|---|---|---|
| **Live** (runtime-connected) | `petron brain serve` → open `http://127.0.0.1:8765/` | the real state of PETRON missions in `$PETRON_HOME`, read-only |
| **Live, interactive** (opt-in) | `petron brain serve --interactive --template SPEC` → open the URL the terminal prints (`…/#k=…`) | the same, plus mission entry on the sphere (see [Mission entry](#mission-entry-interactive-opt-in)) |
| **Visual only** (standalone demo) | open `index.html` directly | the animation; driven only by `window.PetronBrain` calls, never by runtime data |

Live mode exists only behind the bridge: the bridge serves `index.html` with `live.js` added.
The standalone file never connects to anything.

```bash
# from the repository root, with the same PETRON_HOME the CLI uses
PYTHONPATH=apps/petron/src python -m petron brain serve      # or: petron brain serve
```

Then run missions with the normal CLI (`petron experiment e002`, `mission approve`, `mission run`)
and watch the Brain respond.

## States (motion and color are interpolated, ~0.6 s)

| Brain state | Shown when (authoritative runtime event) |
|---|---|
| `idle` | no active mission; mission created; approval recorded but not yet running; terminal states older than 15 s |
| `thinking` | `agent.decision_requested` (the runtime is waiting for a provider decision); mission planning |
| `working` | mission running; tool authorized (`tool.authorization` allow); tool result observed; resumed |
| `verifying` | `agent.verification_started`, mission `verifying`, `verification.result` |
| `awaiting_approval` | mission `awaiting_approval` (nothing is approved until the CLI, or a completed hold on a Brain draft, records it) |
| `success` | mission `completed`, only if the transition is fresher than 15 s; held 4.5 s, then idle |
| `error` | mission `failed` (fresh), tool denied, provider error, execution disabled |
| `disconnected` | the bridge stream is lost (after a 1.5 s grace); never shown as activity |
| `listening` | interactive only: while a typing session on the sphere is actually open (no microphone) |
| `confirming` | interactive only: while the approval hold is in progress; released early, nothing happens |

`activity` is a fixed heuristic per event type (e.g. executing a tool animates more than reading
a file). It is not a measurement: no CPU, token or progress figure is invented.

A mission that is `running` **or `verifying`** whose runner has died is shown as calm `idle` with
the label "missão interrompida; retome pela CLI", never as work and never as success. The V0.2 lease
rules decide: a dead holder, or a released lease on a mission that has not ended, means interrupted.
An agent mission that never took a lease is judged only after its start-up window (its own
`exec_timeout_s` plus 120 s); plan-mode missions never take one and are not judged. Reconnection
replays missed events (old completions map to `idle`) and then applies the authoritative snapshot
(`interrupted` included), so an old success is never celebrated again.

## Bridge security (`apps/petron/src/petron/brain_bridge.py`)

Loopback bind only; fixed asset allowlist (no URL path reaches the filesystem); `Host` must name
the loopback server (DNS-rebinding guard); a present `Origin` must be this server; cross-site
fetches refused; no CORS headers; strict CSP (`default-src 'none'`, self-only scripts); SQLite
opened read-only by the bridge; event payload contents are never forwarded. Without
`--interactive` the bridge is GET/HEAD only: every `POST` is 405 and the page has no input
element at all. There, pointer and keyboard only give parallax and a pulse: no interaction
starts, approves or changes a mission.

## Mission entry (interactive, opt-in)

```bash
# from the repository root
PYTHONPATH=apps/petron/src python -m petron brain serve --interactive \
  --template apps/petron/fixtures/e002/mission.json
# prints: PETRON Brain (interactive) at http://127.0.0.1:8765/#k=<per-run key>
```

Still nothing but the sphere on screen. The gestures:

| Gesture | Effect |
|---|---|
| tap/click the sphere, or Tab then Enter | opens an invisible typing session (`listening`); keys pulse the sphere |
| Enter | sends the request: it becomes a **draft** that waits (`awaiting_approval`) |
| Escape / leaving / 45 s idle | cancels the typing session; nothing is sent |
| press and **hold ~1.8 s** (or hold Space) | approves *that exact draft* (`confirming` while holding); the runtime runs it |
| release early, or a plain tap | nothing is approved |
| Escape on a pending draft | discards it (mission `cancelled`); nothing ran |

**Who decides what** (`apps/petron/src/petron/brain_commands.py`):

- The **operator** chooses the template at the terminal: workspace, tools, writable paths,
  limits, sandbox and provider. The browser contributes only the objective text (≤ 500 chars,
  control characters stripped). The template must be an agent-mode spec. A real (paid) provider
  is refused at startup unless the operator also passes `--allow-real-model`.
- A **draft** is an ordinary agent mission stopped at `awaiting_approval`, with its scope hash
  recorded and a `brain.draft` event (it stores the objective's SHA-256, not a second copy, and
  the identity of the bridge run that created it). Only one Brain draft or run of *this bridge*
  may be active at a time. If preparing the draft fails after the mission was stored, the
  mission is cancelled (`draft_setup_failed`, with a `brain.draft_failed` event) and the request
  is refused with `500 draft_failed`; if even that cancellation cannot be stored the refusal is
  `503 draft_failed_not_cancelled` and the mission is left for the operator (`petron mission stop`).
- **Approval** is a separate, deliberate act bound to the scope hash, recorded as
  `plan.approved` with channel `brain-hold-gesture`. The server re-checks state and hash on
  every command. Through the Brain it applies only to drafts **this bridge run** created: a
  draft made by another bridge sharing the same `PETRON_HOME`, by an earlier run, or with the
  CLI keeps CLI approval (`403 not_a_brain_draft`) and is never offered by this bridge's
  snapshot. The approve answer is `approved`, not `running`: approval is not execution.
- If the runtime **refuses the run after approval** (for example the provider cannot be
  built), the mission ends as `failed` with reason `run_refused:<code>` and a `brain.run_refused`
  event. The approval record is kept, it cannot be reused, and the Brain never shows the mission
  as running or restores it as a pending draft; a new mission is then possible.
- **Execution** goes through the same engine, Tool Gateway, Docker sandbox and verification as
  the CLI. The Brain shows `success` only from the runtime's own `completed` transition. The
  browser never executes anything, never reads SQLite and never names a tool or path.

**Command channel guards** (all must pass; nothing is created otherwise):

- loopback bind and the `Host` guard;
- `Origin` required and equal to this server;
- `Sec-Fetch-Site: same-origin`;
- `Content-Type: application/json`;
- per-run key in `X-Petron-Key`, compared in constant time;
- body ≤ 2 KiB;
- exact JSON key set per action;
- fixed paths: `POST /api/missions`, `…/{id}/approve`, `…/{id}/discard`;
- every refusal closes the HTTP connection (`Connection: close`), so unread body bytes are never
  parsed as another request; a client that stops sending is dropped after 30 s; an unexpected
  failure answers `500 internal_error` without detail.

`localhost` is accepted as a name but the loopback literal `127.0.0.1` is what gets bound.

The key is generated per run (`secrets.token_urlsafe(32)`), printed only in the operator's
terminal, carried in the URL fragment (never sent in a request line, never in a static file),
kept in page memory and removed from the address bar on load. A page opened without it can only
watch.

Limits, stated plainly:

- The key is the only credential. Anyone who can read the operator's terminal or browser memory
  on that machine can act as the operator.
- The gesture proves deliberate intent, not identity.
- No voice. Typing is the only input.

## Voice: interface contract (not implemented)

Not shipped, on purpose: browser speech recognition is not reliably local (Chromium's Web
Speech API sends audio to a remote service). The command channel above is the path a transcript
would take. The contract:

1. **Capture** only after an explicit gesture on the Brain and the browser's microphone
   permission; the Brain shows `listening` only while capture is actually running.
2. **Transcription** by a local engine by default; any remote engine needs explicit opt-in.
3. **Draft, never execute**: the transcript becomes the same draft as typed text
   (`POST /api/missions`), and approval stays the separate hold gesture bound to the scope hash.
4. **No paid model** sees a transcript unless the operator started the bridge with
   `--allow-real-model`.

## Color

Three palettes. None of them has a visible switch:

| Palette | Look |
|---|---|
| `adaptive` (default) | the color energy follows the runtime state (table below) |
| `spectrum` | the approved color edition: one continuous spectrum, the same in every state |
| `mono` | the original black-and-white Brain, pixel-identical to it (verified under reduced motion) |

Choose one with the URL: `http://127.0.0.1:8765/?palette=mono`. In interactive mode it goes before
the key: `/?palette=spectrum#k=…`; the query survives the key's removal. A host can also call
`window.PetronBrain.setPalette('mono' | 'spectrum' | 'adaptive')`. The prototype's name `'state'`
is still accepted, as an alias of `'adaptive'`.

**Adaptive** reinforces the motion that each state already has. The motion still carries the
meaning; the color only reinforces it:

| State | Color energy |
|---|---|
| `idle` | calm, pale, cool white-blue |
| `listening` | cyan / electric blue |
| `thinking` | indigo, blue-violet, a little magenta |
| `working` | richer blue, violet, pink |
| `verifying` | cool white-blue clarity |
| `awaiting_approval` | still, restrained periwinkle |
| `confirming` (hold) | brightening cool blue |
| `success` | the full spectrum bloom (only from a fresh runtime `completed`), then back to calm |
| `error` | restrained warm accents (rose, amber) among muted blues |
| `disconnected` | nearly grey and dimmed, clearly below idle |

How it is built:

- The approved color layer is the prototype's `conic-gradient`, masked to the texture's own
  alpha (`brain-mask.webp`, imported unchanged). Its stops and the canvas particle tints are
  targets per palette and state.
- Those targets are interpolated with the same time constant as the motion, so the sphere
  re-tints smoothly and never flashes.
- At rest no style is written. Colors are cues only; they never claim a mission state that the
  runtime did not report.

**Known limit:** opened directly as a `file://` page, Chromium refuses the mask image (CSS masks
are fetched with CORS). The color layer then stays hidden, and the particles alone carry the
color. Served over HTTP (the bridge, or any static server) everything renders.

## Notes on the approved visual

The prototype's renderer is preserved. Additions: four states, interpolated parameters, an
integrated animation phase (state changes no longer jump the motion), the texture layer
following state luminosity (idle unchanged at 0.92), `pulse()` and `modes`. The approved
palette contains very faint cool tints (halo `rgb(150,160,185)` at ≤2.3 % alpha, particles
`rgb(241,246,255)`); they are kept as approved (measured chroma ≤ 0.36/255 in screenshots).
The color edition (`PETRON-Brain-Colors-source.zip`) contributed only its visual layer: the
spectrum, the mask and the palette API. The bridge, the event mapping, the interpolation and
the phase fix are unchanged, as that prototype's own notes ask.

## Tests

- `apps/petron/tests/test_brain_bridge.py`: mapping, freshness, snapshots, reconnection,
  server refusals (part of the normal suite).
- `apps/petron/tests/test_brain_commands.py`: drafts, hold approval bound to the scope hash,
  CLI missions refused, discard, every command-channel guard (part of the normal suite).
- `apps/petron/tests/e2e/brain_interaction_e2e.py`: real browser, real interactive bridge, real
  E002 in Docker; tap, type, hold, short hold, Escape, Space, no-key page, read-only bridge.
- `apps/petron/tests/e2e/brain_e2e.py`: real browser, real bridge, real E002 + failing mission
  + bridge kill/restart (needs Playwright, Chromium and Docker).
