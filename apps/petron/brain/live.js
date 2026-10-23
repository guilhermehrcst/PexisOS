/**
 * PETRON Brain: live runtime link (loaded only when served by `petron brain serve`).
 *
 * Read-only. It listens to the local bridge's Server-Sent Events and forwards the runtime's own
 * states to window.PetronBrain. It sends nothing, executes nothing and approves nothing.
 *
 *  - snapshot: authoritative current state (applied on every (re)connection);
 *  - runtime:  one stored PETRON event, in sequence order (older sequences are ignored);
 *  - status:   bridge-derived condition from runtime state (e.g. a runner that died).
 * A lost connection becomes the calm `disconnected` state, never activity.
 */
(() => {
  'use strict';

  const brain = window.PetronBrain;
  if (!brain || typeof window.EventSource !== 'function') return;
  const status = document.getElementById('petron-status');
  const canvas = document.getElementById('brain');
  const SUCCESS_HOLD_MS = 4500;
  const DISCONNECT_GRACE_MS = 1500;
  let lastSeq = -1;
  let label = 'conectando ao runtime';
  let successTimer = 0;
  let disconnectTimer = 0;

  function announce(text) {
    label = text || label;
    if (status && status.textContent !== `PETRON: ${label}.`) status.textContent = `PETRON: ${label}.`;
  }

  function show(message) {
    if (!message || typeof message.state !== 'string' || !brain.modes.includes(message.state)) return;
    window.clearTimeout(successTimer);
    brain.setState(message.state);
    if (Number.isFinite(message.activity)) brain.setActivity(message.activity);
    announce(message.label);
    if (message.state === 'success') {
      // A verified completion is celebrated briefly, then the Brain returns to calm.
      successTimer = window.setTimeout(() => {
        if (brain.getState().mode === 'success') { brain.setState('idle'); brain.setActivity(0.25); }
      }, SUCCESS_HOLD_MS);
    }
  }

  function parse(event) {
    try { return JSON.parse(event.data); } catch { return null; }
  }

  const source = new EventSource('/api/events');
  source.addEventListener('snapshot', (event) => {
    const message = parse(event);
    if (!message) return;
    lastSeq = Math.max(lastSeq, Number(message.seq) || 0);
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
        window.clearTimeout(successTimer);
        brain.setState('disconnected');
        brain.setActivity(0.05);
        announce('desconectado do runtime; tentando reconectar');
      }
    }, DISCONNECT_GRACE_MS);
  });
  if (canvas) {
    canvas.addEventListener('focus', () => announce(label));
    canvas.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') announce(label); });
  }
  window.addEventListener('pagehide', () => source.close(), { once: true });
})();
