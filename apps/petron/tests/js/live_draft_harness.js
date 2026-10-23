// Deterministic checks of brain/live.js draft handling, with no browser: the page script runs in a
// Node `vm` against small stubs (EventSource, fetch, DOM, timers). Time only moves when a test moves it.
//
//   node tests/js/live_draft_harness.js [path/to/live.js]
//
// Exit code 0 only if every scenario passes. Each scenario states a property the operator relies on.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const LIVE = process.argv[2] || path.join(__dirname, '..', '..', 'brain', 'live.js');
const SOURCE = fs.readFileSync(LIVE, 'utf8');
const KEY = 'k'.repeat(40);
const SCOPE = 'a'.repeat(64);
const SCOPE_2 = 'b'.repeat(64);
const MODES = ['idle', 'thinking', 'working', 'verifying', 'success', 'error', 'awaiting_approval',
  'disconnected', 'listening', 'confirming'];

function build() {
  const clock = { now: 0, timers: [], seq: 0 };
  const log = { fetches: [], announced: [], states: [] };
  let mode = 'idle';
  const brain = {
    modes: MODES,
    setState: (m) => { mode = m; log.states.push(m); },
    getState: () => ({ mode }),
    setActivity: () => {}, pulse: () => {},
  };
  const handlers = { canvas: {}, input: {} };
  const status = { textContent: '' };
  const canvas = { addEventListener: (n, f) => { handlers.canvas[n] = f; }, focus() {}, setPointerCapture() {} };
  const input = { value: '', addEventListener: (n, f) => { handlers.input[n] = f; }, focus() {} };
  const sources = [];
  class EventSource {
    constructor() { this.listeners = {}; this.readyState = 1; sources.push(this); }
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
    emit(name, data) { for (const fn of this.listeners[name] || []) fn({ data: JSON.stringify(data) }); }
    close() {}
  }
  EventSource.OPEN = 1;
  const responses = [];
  const win = {
    PetronBrain: brain,
    EventSource,
    location: { hash: `#k=${KEY}`, pathname: '/', search: '' },
    history: { replaceState() {} },
    addEventListener() {},
    fetch: async (url, options) => {
      log.fetches.push({ url, body: JSON.parse(options.body) });
      const next = responses.shift() || { ok: true, status: 200, json: {} };
      return { ok: next.ok, status: next.status, json: async () => next.json };
    },
    setTimeout: (fn, ms) => { clock.timers.push({ id: ++clock.seq, at: clock.now + ms, fn, every: 0 }); return clock.seq; },
    setInterval: (fn, ms) => { clock.timers.push({ id: ++clock.seq, at: clock.now + ms, fn, every: ms }); return clock.seq; },
    clearTimeout: (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); },
    clearInterval: (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); },
  };
  const doc = {
    getElementById: (id) => ({ 'petron-status': status, brain: canvas, 'petron-input': input }[id] || null),
    activeElement: null,
  };
  const context = vm.createContext({ window: win, document: doc, performance: { now: () => clock.now },
    EventSource, URLSearchParams, console });
  vm.runInContext(SOURCE, context, { filename: 'live.js' });

  const world = {
    brain, log, status, handlers, input, canvas, sources, responses,
    get source() { return sources[0]; },
    get mode() { return mode; },
    async tick(ms) {
      const end = clock.now + ms;
      for (;;) {
        const due = clock.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        clock.now = due.at;
        if (due.every) due.at += due.every; else clock.timers = clock.timers.filter((t) => t !== due);
        due.fn();
        await flush();
      }
      clock.now = end;
      await flush();
    },
    snapshot(extra) {
      world.source.emit('snapshot', { type: 'snapshot', seq: ++world.seq, mission_id: null, mission_state: null,
        interrupted: false, state: 'idle', activity: 0.25, label: 'em repouso', interactive: true, draft: null,
        ...extra });
    },
    runtime(extra) {
      world.source.emit('runtime', { type: 'runtime', seq: ++world.seq, at: 'x', activity: 0.4, label: 'l', ...extra });
    },
    seq: 100,
    async hold() {                      // press and hold the sphere long enough to approve, then release
      const down = handlers.canvas.pointerdown({ preventDefault() {}, pointerId: 1 });
      await flush();
      await down;
      await world.tick(2000);
      handlers.canvas.pointerup();
      await flush();
      handlers.canvas.click();                // a browser fires click when the same pointer is released
      await flush();
    },
    async tap() { handlers.canvas.click(); await flush(); },
    async escape() { await handlers.canvas.keydown({ key: 'Escape' }); await flush(); },
    async type(text) {
      input.value = text;
      handlers.input.keydown({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {} });
      await flush();
    },
  };
  return world;
}

async function flush() { for (let i = 0; i < 8; i += 1) await Promise.resolve(); }

const MISSION = 'brain-20260101000000-aaaaaa';
const pending = (id = MISSION, scope = SCOPE) =>
  ({ mission_id: id, state: 'awaiting_approval', approved: false, scope_sha256: scope });

const scenarios = [];
const scenario = (name, fn) => scenarios.push([name, fn]);

async function ready(extra) {
  const world = build();
  world.snapshot(extra);
  await world.tick(1);
  return world;
}

// The draft is created the way a user does it: tap, type, Enter. The server answers with the draft.
async function drafted(id = MISSION, scope = SCOPE) {
  const world = await ready();
  world.responses.push({ ok: true, status: 200, json: { mission_id: id, scope_sha256: scope, state: 'awaiting_approval' } });
  await world.tap();
  assert.equal(world.mode, 'listening');
  await world.type('corrija o defeito');
  assert.equal(world.log.fetches[0].url, '/api/missions');
  await world.tick(2000);
  return world;
}

async function startsANewMission(world) {
  await world.tick(2000);                               // any refusal flash passes
  await world.tap();
  return world.mode === 'listening';
}

scenario('a pending draft of this bridge is recovered after a reload and approves by hold', async () => {
  const world = await ready({ mission_id: MISSION, mission_state: 'awaiting_approval',
    state: 'awaiting_approval', draft: pending() });
  await world.hold();
  assert.equal(world.log.fetches.length, 1);
  assert.match(world.log.fetches[0].url, /\/api\/missions\/brain-20260101000000-aaaaaa\/approve$/);
  assert.deepEqual(world.log.fetches[0].body, { scope_sha256: SCOPE });
});

scenario('not_awaiting_approval on approve clears the stale draft: a new mission can be started', async () => {
  const world = await drafted();
  world.responses.push({ ok: false, status: 409, json: { error: 'not_awaiting_approval' } });
  await world.hold();                                   // it ended elsewhere (for example a CLI cancel)
  assert.ok(await startsANewMission(world), 'still blocked by the obsolete draft');
  await world.type('outra tarefa');
  assert.equal(world.log.fetches.at(-1).url, '/api/missions');
});

scenario('unknown_mission on discard clears the stale draft', async () => {
  const world = await drafted();
  world.responses.push({ ok: false, status: 404, json: { error: 'unknown_mission' } });
  await world.escape();
  assert.ok(await startsANewMission(world), 'still blocked by the obsolete draft');
});

scenario('a draft that belongs to another bridge (not_a_brain_draft) is dropped, not retried forever', async () => {
  const world = await drafted();
  world.responses.push({ ok: false, status: 403, json: { error: 'not_a_brain_draft' } });
  await world.hold();
  assert.ok(await startsANewMission(world));
});

scenario('a refusal that is not about the draft keeps it (a wrong scope hash can be retried)', async () => {
  const world = await drafted();
  world.responses.push({ ok: false, status: 409, json: { error: 'plan_hash_mismatch' } });
  await world.hold();
  assert.equal(await startsANewMission(world), false);
  assert.match(world.status.textContent, /segure/);
});

scenario('releasing the sphere after an approval hold never opens a typing session', async () => {
  const world = await drafted();
  world.responses.push({ ok: true, status: 200, json: { mission_id: MISSION, approved: SCOPE, state: 'approved' } });
  // The runtime refuses the run at once: the mission fails before the finger is lifted.
  const approving = world.hold();
  await world.tick(1900);
  world.runtime({ mission_id: MISSION, kind: 'mission.state', state: 'error' });
  await approving;
  assert.notEqual(world.mode, 'listening', 'the release click was taken for a tap');
});

scenario('a genuine tap after a hold still starts a request', async () => {
  const world = await drafted();
  world.responses.push({ ok: true, status: 200, json: { mission_id: MISSION, approved: SCOPE, state: 'approved' } });
  await world.hold();
  world.runtime({ mission_id: MISSION, kind: 'mission.state', state: 'error' });   // the draft is over
  await world.tick(9000);
  await world.tap();
  assert.equal(world.mode, 'listening');
});

scenario('a snapshot without a draft (cancelled while disconnected) forgets the local draft', async () => {
  const world = await drafted();
  world.snapshot({ draft: null, mission_state: 'cancelled', mission_id: MISSION });
  await world.tick(1);
  assert.ok(await startsANewMission(world), 'the draft ended while the page was away and still blocks');
});

scenario('a snapshot that still shows the pending draft keeps it', async () => {
  const world = await drafted();
  world.snapshot({ draft: pending(), mission_state: 'awaiting_approval', state: 'awaiting_approval' });
  await world.hold();
  assert.equal(world.log.fetches.length, 2);            // creation + approval
  assert.match(world.log.fetches[1].url, /\/approve$/);
});

scenario('a running mission of this bridge is preserved: no second mission while it runs', async () => {
  const world = await drafted();
  await world.hold();                                   // approved
  world.snapshot({ mission_state: 'running', state: 'working', mission_id: MISSION,
    draft: { mission_id: MISSION, state: 'running', approved: true } });
  await world.tick(1);
  await world.tap();
  assert.notEqual(world.mode, 'listening');
  assert.match(world.status.textContent, /aguarde/);
});

scenario('an old runtime event never brings back a draft that has ended', async () => {
  const world = await drafted();
  await world.escape();                                  // discarded: the draft is over
  world.runtime({ mission_id: MISSION, kind: 'mission.created', state: 'idle' });
  world.runtime({ mission_id: MISSION, kind: 'mission.state', state: 'awaiting_approval' });
  await world.tick(1);
  const before = world.log.fetches.length;
  await world.hold();
  assert.equal(world.log.fetches.length, before, 'an ended draft was approved from an old event');
});

scenario('a reconnection (new snapshot) replaces the local view with the server\'s newer draft', async () => {
  const world = await drafted(MISSION, SCOPE);
  world.snapshot({ draft: pending('brain-20260101000001-bbbbbb', SCOPE_2) });
  await world.hold();
  const approval = world.log.fetches.at(-1);
  assert.match(approval.url, /brain-20260101000001-bbbbbb\/approve$/);
  assert.deepEqual(approval.body, { scope_sha256: SCOPE_2 });
});

scenario('a terminal runtime event for the draft mission ends it', async () => {
  const world = await drafted();
  world.runtime({ mission_id: MISSION, kind: 'mission.state', state: 'idle' });  // cancelled
  await world.tick(1);
  assert.ok(await startsANewMission(world));
});

scenario('a terminal failure is held briefly and then settles to calm idle', async () => {
  const world = await ready();
  world.runtime({ mission_id: 'm', kind: 'mission.state', state: 'error' });
  await world.tick(1);
  assert.equal(world.mode, 'error');
  await world.tick(9000);
  assert.equal(world.mode, 'idle', 'the error animation never ended');
});

scenario('a transient error (a denied action) does not reset the state by itself', async () => {
  const world = await ready();
  world.runtime({ mission_id: 'm', kind: 'tool.authorization', state: 'error' });
  await world.tick(60000);
  assert.equal(world.mode, 'error');
});

(async () => {
  let failed = 0;
  for (const [name, fn] of scenarios) {
    try { await fn(); console.log(`PASS ${name}`); } catch (error) {
      failed += 1; console.log(`FAIL ${name}\n     ${String(error.message).split('\n')[0]}`);
    }
  }
  console.log(`\n${scenarios.length - failed}/${scenarios.length} scenarios passed (${LIVE})`);
  process.exit(failed ? 1 : 0);
})();
