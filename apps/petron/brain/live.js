/**
 * PETRON Brain: live runtime link (loaded only when served by `petron brain serve`).
 *
 * Read side (always): listens to the bridge's Server-Sent Events and forwards the runtime's own
 * states to window.PetronBrain.
 *  - snapshot: authoritative current state (applied on every (re)connection);
 *  - runtime:  one stored PETRON event, in sequence order (older sequences are ignored);
 *  - status:   bridge-derived condition from runtime state (e.g. a runner that died).
 * A lost connection becomes the calm `disconnected` state, never activity.
 *
 * Interaction (only when the bridge runs with --interactive AND the page was opened with the
 * per-run key the operator's terminal printed after `#k=`):
 *  - tap / click the sphere, or Enter: an invisible input session opens (`listening`);
 *    typing pulses the sphere; Enter sends the request; Escape or leaving cancels it;
 *  - the request becomes a mission draft that waits for approval (the server decides its scope);
 *  - press and hold the sphere ~1.8 s (or hold Space) to approve that exact draft; releasing
 *    early cancels; Escape discards the draft. A short tap never approves anything.
 * Nothing is executed by this script: the server records the approval and runs the mission.
 */
(() => {
  'use strict';

  const brain = window.PetronBrain;
  if (!brain || typeof window.EventSource !== 'function') return;
  const status = document.getElementById('petron-status');
  const canvas = document.getElementById('brain');
  const input = document.getElementById('petron-input');
  const SUCCESS_HOLD_MS = 4500;
  const ERROR_HOLD_MS = 8000;       // a terminal failure is shown, then calm: it is never an endless alarm
  const DISCONNECT_GRACE_MS = 1500;
  const HOLD_MS = 1800;
  const IDLE_INPUT_MS = 45000;

  // The key lives only in memory; it is removed from the address bar immediately.
  const key = new URLSearchParams(window.location.hash.slice(1)).get('k') || '';
  if (window.location.hash) window.history.replaceState(null, '', window.location.pathname + window.location.search);

  let lastSeq = -1;
  let label = 'conectando ao runtime';
  let successTimer = 0;
  let disconnectTimer = 0;
  let connected = false;
  let interactive = false;
  let view = null;            // last authoritative message from the runtime
  let session = 'none';       // none | typing | holding
  let draft = null;           // { mission_id, scope_sha256, approved }: the server's pending draft of this bridge
  let submitting = null;      // in-flight draft request (gestures wait for it: no race with the stream)
  let pointerHeld = false;
  let swallowClick = false;   // the click a browser fires when a hold gesture ends is not a tap
  let spaceHeld = false;
  let holdStart = 0;
  let holdTimer = 0;
  let idleTimer = 0;
  let lastPulse = 0;

  function announce(text) {
    label = text || label;
    if (status && status.textContent !== `PETRON: ${label}.`) status.textContent = `PETRON: ${label}.`;
  }

  function apply(message) {
    window.clearTimeout(successTimer);
    brain.setState(message.state);
    if (Number.isFinite(message.activity)) brain.setActivity(message.activity);
    // Success, and a terminal failure, are held briefly and then settle to calm idle. Other errors (a denied
    // action) are followed by further events and need no timer.
    const hold = message.state === 'success' ? SUCCESS_HOLD_MS
      : (message.state === 'error' && message.kind === 'mission.state' ? ERROR_HOLD_MS : 0);
    if (hold) {
      successTimer = window.setTimeout(() => {
        if (brain.getState().mode === message.state) { brain.setState('idle'); brain.setActivity(0.25); }
      }, hold);
    }
  }

  function show(message) {
    if (!message || typeof message.state !== 'string' || !brain.modes.includes(message.state)) return;
    view = message;
    // Forget the draft only when its mission really ended (a terminal mission.state transition);
    // other events that map to calm states, such as "mission created", must not clear it.
    if (draft && message.mission_id === draft.mission_id && message.kind === 'mission.state'
        && ['success', 'error', 'idle'].includes(message.state)) draft = null;
    if (session !== 'none') return;   // a local gesture is in progress; resume the view afterwards
    apply(message);
    announce(message.label);
  }

  function restore() {
    session = 'none';
    if (view) { apply(view); announce(view.label); } else { brain.setState('idle'); }
  }

  function parse(event) {
    try { return JSON.parse(event.data); } catch { return null; }
  }

  // The snapshot is the server's account of the one draft this bridge has pending or running. The local
  // draft is made to match it on every (re)connection: a draft that ended or was cancelled elsewhere while
  // this page was away is forgotten, a pending or running one is kept (or recovered after a reload). Runtime
  // events never create a draft, so an old event cannot bring back one that has ended.
  function reconcileDraft(owned) {
    if (!owned || typeof owned.mission_id !== 'string') {
      if (!submitting) draft = null;
      return;
    }
    const scope = /^[0-9a-f]{64}$/.test(owned.scope_sha256 || '') ? owned.scope_sha256 : null;
    const approved = Boolean(owned.approved) || !scope;   // no pending scope: nothing left to approve here
    if (draft && draft.mission_id === owned.mission_id) {
      draft.approved = draft.approved || approved;
      if (scope) draft.scope_sha256 = scope;
    } else {
      draft = { mission_id: owned.mission_id, scope_sha256: scope, approved };
    }
  }

  // ---------------------------------------------------------------- read side
  const source = new EventSource('/api/events');
  source.addEventListener('snapshot', (event) => {
    const message = parse(event);
    if (!message) return;
    connected = true;
    interactive = Boolean(message.interactive) && key.length >= 32 && Boolean(input);
    lastSeq = Math.max(lastSeq, Number(message.seq) || 0);
    reconcileDraft(message.draft);
    show(message);
  });
  source.addEventListener('runtime', (event) => {
    const message = parse(event);
    if (!message || !(Number(message.seq) > lastSeq)) return;  // ordered, no replays
    lastSeq = Number(message.seq);
    if (message.state) show(message);
  });
  source.addEventListener('status', (event) => show(parse(event)));
  source.addEventListener('open', () => window.clearTimeout(disconnectTimer));
  source.addEventListener('error', () => {
    window.clearTimeout(disconnectTimer);
    disconnectTimer = window.setTimeout(() => {
      if (source.readyState !== EventSource.OPEN) {
        connected = false;
        if (session === 'typing') cancelTyping(false);
        if (session === 'holding') abortHold(false);
        window.clearTimeout(successTimer);
        brain.setState('disconnected');
        brain.setActivity(0.05);
        announce('desconectado do runtime; tentando reconectar');
      }
    }, DISCONNECT_GRACE_MS);
  });
  window.addEventListener('pagehide', () => source.close(), { once: true });

  // ---------------------------------------------------------------- commands
  async function command(path, body) {
    const response = await window.fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Petron-Key': key },
      body: JSON.stringify(body),
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) throw new Error((payload && payload.error) || `http_${response.status}`);
    return payload;
  }

  function refusal(reason) {
    brain.setState('error');
    announce(`pedido recusado (${reason})`);
    window.setTimeout(restore, 1600);
  }

  // The server answered that this draft is not (or no longer) pending: it ended elsewhere (a CLI cancel, a
  // restart, another run). Keeping it would block every new mission until a reload.
  const STALE_DRAFT = new Set(['not_awaiting_approval', 'unknown_mission', 'not_a_brain_draft']);
  function forgetIfStale(error, target) {
    if (STALE_DRAFT.has(error.message) && draft === target) draft = null;
  }

  const busy = () => view && ['thinking', 'working', 'verifying'].includes(view.state);
  // A draft is pending from the server's own answer; the server re-checks state and scope hash
  // on every command, so the client never has to infer it from the event stream's timing.
  const awaitingDraft = () => Boolean(draft && !draft.approved);
  async function settled() {
    if (submitting) { try { await submitting; } catch { /* reported by submit() */ } }
  }

  // ---------------------------------------------------------------- typing session
  function startTyping() {
    session = 'typing';
    input.value = '';
    input.focus({ preventScroll: true });
    brain.setState('listening');
    brain.setActivity(0.35);
    announce('escutando: digite o pedido; Enter envia, Escape cancela');
    armIdle();
  }

  function armIdle() {
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => { if (session === 'typing') cancelTyping(true); }, IDLE_INPUT_MS);
  }

  function cancelTyping(say) {
    window.clearTimeout(idleTimer);
    input.value = '';
    session = 'none';
    if (document.activeElement === input) canvas.focus({ preventScroll: true });
    restore();
    if (say) announce('pedido cancelado');
  }

  async function submit() {
    const text = input.value.trim();
    window.clearTimeout(idleTimer);
    input.value = '';
    session = 'none';
    canvas.focus({ preventScroll: true });
    if (!text) { restore(); return; }
    brain.pulse();
    submitting = command('/api/missions', { objective: text });
    try {
      draft = { ...(await submitting), approved: false };
      announce('missão criada e aguardando autorização: segure a esfera por 2 segundos para autorizar; '
        + 'Escape descarta');
    } catch (error) {
      refusal(error.message);
    } finally {
      submitting = null;
    }
  }

  if (input) {
    input.addEventListener('input', () => {
      armIdle();
      const now = performance.now();
      if (now - lastPulse > 160) { brain.pulse(); lastPulse = now; }
      brain.setActivity(Math.min(0.85, 0.35 + input.value.length / 400));
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancelTyping(true); }
    });
    input.addEventListener('blur', () => { if (session === 'typing') cancelTyping(true); });
  }

  // ---------------------------------------------------------------- hold to approve
  function startHold() {
    session = 'holding';
    holdStart = performance.now();
    brain.setState('confirming');
    brain.setActivity(0.15);
    announce('autorizando: continue segurando');
    window.clearInterval(holdTimer);
    holdTimer = window.setInterval(() => {
      const progress = (performance.now() - holdStart) / HOLD_MS;
      brain.setActivity(Math.min(0.95, 0.15 + 0.8 * progress));
      if (progress >= 1) completeHold();
    }, 50);
  }

  function abortHold(say) {
    window.clearInterval(holdTimer);
    if (session !== 'holding') return;
    restore();
    if (say) announce('autorização cancelada; nada foi executado');
  }

  async function completeHold() {
    window.clearInterval(holdTimer);
    session = 'none';
    const target = draft;
    brain.pulse();
    if (!target) { restore(); return; }
    try {
      await command(`/api/missions/${target.mission_id}/approve`, { scope_sha256: target.scope_sha256 });
      target.approved = true;
      announce('autorização registrada; aguardando o runtime iniciar a missão');
    } catch (error) {
      forgetIfStale(error, target);
      refusal(error.message);
    }
  }

  async function discard() {
    const target = draft;
    if (!target) return;
    try {
      await command(`/api/missions/${target.mission_id}/discard`, {});
      draft = null;
      announce('rascunho descartado; nada foi executado');
    } catch (error) {
      forgetIfStale(error, target);
      refusal(error.message);
    }
  }

  // ---------------------------------------------------------------- gestures
  function activate() {
    if (!interactive || !connected) { announce(label); return; }
    if (awaitingDraft()) { announce('segure a esfera por 2 segundos para autorizar; Escape descarta'); return; }
    if (busy() || draft || submitting) { announce('aguarde: há uma missão em andamento'); return; }
    startTyping();
  }

  if (canvas) {
    canvas.addEventListener('focus', () => announce(label));
    canvas.addEventListener('pointerdown', async (e) => {
      pointerHeld = true;
      swallowClick = false;
      if (!(interactive && connected && session === 'none' && (awaitingDraft() || submitting))) return;
      e.preventDefault();
      canvas.setPointerCapture?.(e.pointerId);
      await settled();
      if (pointerHeld && awaitingDraft() && session === 'none') { swallowClick = true; startHold(); }
    });
    const release = () => { pointerHeld = false; if (session === 'holding') abortHold(true); };
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('lostpointercapture', release);
    canvas.addEventListener('click', () => {
      if (swallowClick) { swallowClick = false; return; }   // releasing after a hold: never a new request
      if (session === 'none') activate();
    });
    canvas.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') { activate(); return; }
      if (e.key === ' ' && !e.repeat) {
        spaceHeld = true;
        if (!(interactive && connected && session === 'none')) return;
        await settled();
        if (spaceHeld && awaitingDraft() && session === 'none') startHold();
      } else if (e.key === 'Escape' && session === 'none') {
        await settled();
        if (awaitingDraft()) discard();
      }
    });
    canvas.addEventListener('keyup', (e) => {
      if (e.key !== ' ') return;
      spaceHeld = false;
      if (session === 'holding') abortHold(true);
    });
  }
})();
