/**
 * PETRON Brain V0.1 (runtime-ready, color edition)
 * A self-contained, CPU-friendly particle sculpture with optional colors.
 * No dependencies, remote requests, sensors, model calls or visible controls.
 *
 * Integration API (V0.1 calls unchanged; additions marked +):
 *   window.PetronBrain.setState(mode)   idle | thinking | working | success | error
 *                                       + listening | verifying | awaiting_approval | disconnected
 *                                       + confirming (local press-and-hold feedback only)
 *   window.PetronBrain.setActivity(0..1)
 *   window.PetronBrain.pause(); window.PetronBrain.resume();
 *   window.PetronBrain.getState()
 * + window.PetronBrain.pulse()          one discreet ring, as on touch
 * + window.PetronBrain.modes            the accepted mode names
 * + window.PetronBrain.setPalette(p)    adaptive (default) | spectrum | mono  ('state' = adaptive)
 * + window.PetronBrain.getPalette()     also in getState().palette; initial value from ?palette=
 *
 * The Brain only draws what a host tells it. It never infers mission status by itself.
 */
(() => {
  'use strict';

  const canvas = document.getElementById('brain');
  if (!canvas) return;
  // The approved texture layer follows the state's luminosity too (idle keeps its 0.92).
  const reference = document.querySelector('.brain-reference');
  let referenceOpacity = -1;
  // The approved color layer: a conic spectrum masked to the texture's own alpha.
  const chromaLayer = document.querySelector('.brain-chroma');
  let chromaKey = '';
  const context = canvas.getContext('2d', { alpha: true, desynchronized: true });
  if (!context) return;

  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  const TAU = Math.PI * 2;
  // The five approved modes keep their exact values. `flow` scales surface turbulence
  // (1 = approved behaviour); lower is more coherent, higher more disturbed.
  const MODES = Object.freeze({
    idle: { speed: 0.18, shimmer: 0.10, breathe: 0.010, glow: 1, flow: 1 },
    thinking: { speed: 0.43, shimmer: 0.23, breathe: 0.017, glow: 1.13, flow: 1 },
    working: { speed: 0.58, shimmer: 0.32, breathe: 0.025, glow: 1.16, flow: 1 },
    success: { speed: 0.27, shimmer: 0.14, breathe: 0.020, glow: 1.24, flow: 1 },
    error: { speed: 0.12, shimmer: 0.06, breathe: 0.006, glow: 0.78, flow: 1 },
    listening: { speed: 0.30, shimmer: 0.16, breathe: 0.022, glow: 1.08, flow: 1.2 },
    verifying: { speed: 0.32, shimmer: 0.07, breathe: 0.012, glow: 1.12, flow: 0.45 },
    awaiting_approval: { speed: 0.08, shimmer: 0.05, breathe: 0.007, glow: 0.94, flow: 0.6 },
    disconnected: { speed: 0.04, shimmer: 0.02, breathe: 0.003, glow: 0.52, flow: 0.3 },
    // Local gesture feedback (press-and-hold to approve): particles gather and brighten.
    confirming: { speed: 0.22, shimmer: 0.04, breathe: 0.030, glow: 1.18, flow: 0.3 },
  });
  const KEYS = ['speed', 'shimmer', 'breathe', 'glow', 'flow'];

  // Color. Three palettes, never a visible switch:
  //   mono      the original black-and-white identity (its exact colors);
  //   spectrum  the approved continuous color treatment, the same in every state;
  //   adaptive  (default) the color energy follows the state the host reports.
  // A look is a target, interpolated like the motion parameters, so a state change re-tints
  // and never flashes. Motion still carries the state; color only reinforces it.
  const PALETTES = Object.freeze(['adaptive', 'spectrum', 'mono']);
  const MONO = { particle: [241, 246, 255], strand: [230, 238, 248], dust: [250, 251, 255], ring: [250, 250, 255] };
  const STRAND_TINT = [0, 3, 4, 5, 2, 0];   // which spectrum tint each rim filament takes
  const DUST_TINT = [1, 0, 3, 4, 4];
  function hsl(h, s, l) {
    const f = (n) => {
      const k = (n + h / 30) % 12;
      return Math.round(255 * (l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
    };
    return [f(0), f(8), f(4)];
  }
  const hex = (v) => [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16));
  // The approved spectrum, exactly as in the color prototype (gradient stops, particle hues).
  const SPECTRUM = Object.freeze({
    stops: ['#1f77f8', '#52ddfb', '#62cffe', '#c982f7', '#ed64a4', '#ffb36b', '#65ffd1'].map(hex),
    tints: [210, 188, 180, 270, 320, 31, 163].map((h) => hsl(h, 0.95, 0.76)),
    amount: 1, chroma: 0.97, base: 0.27,
  });
  const MONOCHROME = Object.freeze({ ...SPECTRUM, amount: 0, chroma: 0, base: 0.92 });
  // Adaptive: seven hues per state (spread around the sphere), how saturated they are, how
  // strong the color layer is and how much of the white texture remains.
  function tone(hues, sat, chroma, base) {
    return Object.freeze({
      stops: hues.map((h) => hsl(h, 0.95 * sat, 0.62 + 0.18 * (1 - sat))),
      tints: hues.map((h) => hsl(h, 0.95 * sat, 0.76 + 0.1 * (1 - sat))),
      amount: 1, chroma, base,
    });
  }
  const ADAPTIVE = Object.freeze({
    idle: tone([214, 206, 222, 232, 212, 198, 218], 0.38, 0.62, 0.5),        // calm, pale, cool
    listening: tone([196, 188, 202, 212, 192, 184, 206], 0.95, 0.95, 0.3),   // cyan / electric blue
    thinking: tone([226, 238, 250, 262, 290, 246, 232], 0.82, 0.95, 0.27),   // indigo, violet, a little magenta
    working: tone([212, 240, 272, 304, 330, 286, 198], 1, 1, 0.27),          // blue, violet, pink
    verifying: tone([204, 198, 212, 194, 216, 206, 200], 0.5, 0.8, 0.45),    // cool white-blue clarity
    awaiting_approval: tone([232, 240, 246, 236, 228, 244, 238], 0.48, 0.6, 0.42),  // still, restrained
    success: SPECTRUM,                                                       // the full bloom
    error: tone([8, 214, 24, 226, 2, 208, 32], 0.66, 0.8, 0.3),           // restrained warm accents
    disconnected: tone([220, 222, 216, 226, 218, 212, 224], 0.14, 0.3, 0.5), // subdued (and dimmed by glow)
    confirming: tone([198, 208, 218, 204, 194, 212, 206], 0.65, 0.9, 0.4),
  });
  const canonical = (value) => (value === 'state' ? 'adaptive' : value);
  const requested = canonical(new URLSearchParams(window.location.search).get('palette'));
  let palette = PALETTES.includes(requested) ? requested : 'adaptive';
  const lookFor = () => (palette === 'mono' ? MONOCHROME : palette === 'spectrum' ? SPECTRUM : ADAPTIVE[mode] || SPECTRUM);
  const copyLook = (l) => ({ stops: l.stops.map((c) => [...c]), tints: l.tints.map((c) => [...c]),
    amount: l.amount, chroma: l.chroma, base: l.base });
  const mix = (a, b, f) => [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  const rgba = (c, alpha) => `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${alpha})`;

  let width = 1;
  let height = 1;
  let scale = 1;
  let radius = 1;
  let cloud = [];
  let aura = [];
  let mode = 'idle';
  let activity = 0.25;
  let raf = 0;
  let stopped = false;
  let lastRender = -Infinity;
  let now = 0;
  let pointerX = 0;
  let pointerY = 0;
  let smoothX = 0;
  let smoothY = 0;
  let pulseTime = -100;
  let tick = 0;
  // Smoothly interpolated parameters and an integrated phase: changing mode changes the rate
  // of motion, never jumps the current position.
  const cur = { ...MODES.idle };
  let look = null;   // current (interpolated) colors; set once the initial mode is known
  let phase = 0;
  let lastDrawMs = null;

  // Reproducible procedural composition. No Math.random() means consistent identity.
  function makeRandom(seed) {
    let value = seed >>> 0;
    return () => {
      value += 0x6d2b79f5;
      let t = value;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const rnd = makeRandom(146328);

  function buildCloud() {
    cloud = [];
    aura = [];
    const mobile = Math.min(width, height) < 600;
    const density = mobile ? 0.68 : 1;
    const rings = Math.round(98 * Math.sqrt(density));
    const samples = Math.round(330 * Math.sqrt(density));

    // Geodesic textile: organized latitude rows, not a random point ball.
    // The organic edge is formed by layered spatial frequencies on the surface.
    for (let i = 0; i < rings; i++) {
      const phi = (i + 0.5) / rings * Math.PI;
      const sinP = Math.sin(phi);
      const cosP = Math.cos(phi);
      const count = Math.max(14, Math.round(samples * sinP));
      const rowPhase = (i % 2) * 0.5;
      for (let j = 0; j < count; j++) {
        if (rnd() < 0.095) continue;
        const theta = TAU * (j + rowPhase) / count;
        const noise = (rnd() - 0.5);
        const surface = 1
          + 0.029 * Math.sin(theta * 7.0 + phi * 5.0)
          + 0.016 * Math.sin(theta * 11.0 - phi * 8.0)
          + 0.012 * Math.sin(theta * 22.0 + phi * 2.0)
          + 0.006 * noise;
        const x = sinP * Math.cos(theta) * surface;
        const y = cosP * surface;
        const z = sinP * Math.sin(theta) * surface;
        const wave = Math.sin(theta * 18.0 + phi * 8.5);
        const band = (0.5 + 0.5 * Math.sin(theta * 15 + phi * 20));
        cloud.push({ x, y, z, theta, phi, noise, wave, band,
          blink: rnd() * TAU, small: rnd() < 0.82, type: 0 });
      }
    }

    // Fine stellar dust around the rough silhouette.
    const dustCount = Math.round((mobile ? 1950 : 4200) * Math.max(0.75, radius / 385));
    for (let i = 0; i < dustCount; i++) {
      const a = rnd() * TAU;
      const g = rnd();
      const out = Math.pow(g, 4) * 0.17;
      const shell = 0.925 + out + (rnd() - 0.5) * 0.09;
      aura.push({ a, d: shell, phase: rnd() * TAU, bright: Math.pow(rnd(), 1.1), size: rnd() });
    }
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    width = Math.max(1, Math.round(rect.width));
    height = Math.max(1, Math.round(rect.height));
    // Clamp pixel density to avoid overloading older GPUs / mobile browsers.
    scale = Math.min(window.devicePixelRatio || 1, Math.min(width, height) < 500 ? 1.5 : 1.75);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    context.setTransform(scale, 0, 0, scale, 0, 0);
    radius = Math.min(width * (width < 700 ? 0.42 : 0.355), height * 0.425, 545);
    buildCloud();
    draw(now);
  }

  function draw(ms) {
    now = ms;
    tick++;
    const seconds = ms * 0.001;
    const energy = clamp(activity, 0, 1);
    const reduced = motionQuery.matches;
    const target = MODES[mode];
    const dt = lastDrawMs === null ? 0 : clamp((ms - lastDrawMs) * 0.001, 0, 0.1);
    lastDrawMs = ms;
    const k = reduced ? 1 : 1 - Math.exp(-dt / 0.6);  // ~0.6 s time constant
    for (const key of KEYS) cur[key] += (target[key] - cur[key]) * k;
    const cfg = cur;
    const want = lookFor();
    if (!look) look = copyLook(want);
    for (let i = 0; i < 7; i++) {
      look.stops[i] = mix(look.stops[i], want.stops[i], k);
      look.tints[i] = mix(look.tints[i], want.tints[i], k);
    }
    for (const key of ['amount', 'chroma', 'base']) look[key] += (want[key] - look[key]) * k;
    phase += dt * cfg.speed * (1 + 0.4 * (energy - 0.25));
    const t = reduced ? 0 : phase;
    if (reference) {
      const opacity = clamp(look.base * cfg.glow, 0.1, 1);
      if (Math.abs(opacity - referenceOpacity) > 0.004) {
        reference.style.opacity = opacity.toFixed(3);
        referenceOpacity = opacity;
      }
    }
    if (chromaLayer) {
      // Written only when it visibly changes (during transitions), never every frame at rest.
      const opacity = clamp(look.chroma * cfg.glow, 0, 1).toFixed(2);
      const stops = look.stops.map((c) => rgba(c, 1));
      const key = opacity + stops.join('');
      if (key !== chromaKey) {
        chromaKey = key;
        chromaLayer.style.opacity = opacity;
        stops.forEach((c, i) => chromaLayer.style.setProperty(`--c${i}`, c));
      }
    }
    const breath = reduced ? 1 : 1 + cfg.breathe * Math.sin(seconds * 1.05 + energy);
    const cx = width / 2;
    const cy = height / 2;

    context.clearRect(0, 0, width, height);
    smoothX += (pointerX - smoothX) * 0.032;
    smoothY += (pointerY - smoothY) * 0.032;

    const angY = -0.25 + t * 0.065 + smoothX * 0.22;
    const angX = 0.20 + 0.08 * Math.sin(t * 0.19) - smoothY * 0.19;
    const sy = Math.sin(angY), cyy = Math.cos(angY);
    const sx = Math.sin(angX), cxx = Math.cos(angX);
    const R = radius * breath;

    // Soft lens glow behind the bright limb. The center remains black.
    context.save();
    const halo = context.createRadialGradient(cx, cy, R * 0.67, cx, cy, R * 1.24);
    halo.addColorStop(0, 'rgba(0,0,0,0)');
    halo.addColorStop(0.53, `rgba(150,160,185,${0.008 * cfg.glow})`);
    halo.addColorStop(0.78, `rgba(210,220,235,${0.023 * cfg.glow})`);
    halo.addColorStop(1, 'rgba(0,0,0,0)');
    context.fillStyle = halo;
    context.fillRect(cx - R * 1.25, cy - R * 1.25, R * 2.5, R * 2.5);
    context.restore();

    // Renderer uses 11 brightness buckets to batch thousands of particles.
    // Many faint interior dots, densely concentrated luminous outline.
    // With color, seven hue sectors (around the sphere) times the same 11 brightness buckets.
    const nHues = look.amount > 0.002 ? 7 : 1;
    const buckets = Array.from({ length: 11 * nHues }, () => []);
    const frame = reduced ? 0 : t;
    const flutter = reduced ? 0 : cfg.shimmer;
    const spin = (reduced ? 0 : seconds * 0.06);
    const spinS = Math.sin(spin), spinC = Math.cos(spin);
    for (let i = 0; i < cloud.length; i++) {
      const p = cloud[i];
      const flow = (reduced ? 0 : 0.012 * cfg.flow * Math.sin(frame * 0.9 + p.theta * 9 + p.phi * 7));
      const r = 1 + flow + (reduced ? 0 : 0.003 * Math.sin(frame * 1.3 + p.phi * 19));
      let x = p.x * r;
      let y = p.y * r;
      let z = p.z * r;
      const x1 = x * cyy - z * sy;
      const z1 = x * sy + z * cyy;
      const y1 = y * cxx - z1 * sx;
      const z2 = y * sx + z1 * cxx;
      const xx = x1 * spinC - y1 * spinS;
      const yy = x1 * spinS + y1 * spinC;
      const planar = Math.sqrt(xx * xx + yy * yy);
      const limb = clamp((planar - 0.66) / 0.33, 0, 1);
      const rim = Math.pow(limb, 1.65);
      // Speckle inside the orb; silhouette is brighter by an order of magnitude.
      const facet = clamp(0.66 + 0.27 * p.wave + 0.19 * p.band, 0.19, 1.07);
      const front = z2 > 0 ? 1 : 0.76;
      const shimmer = 1 + flutter * Math.sin(p.blink * 3 + frame * 5 + p.theta * 10);
      let alpha = (0.11 + rim * 0.55 + 0.06 * p.band) * facet * front * shimmer * cfg.glow;
      // Bright crests trace irregular waves around the surface, like fluid nerve pathways.
      if (rim > 0.25 && p.wave > 0.63) alpha *= 1.43;
      if (p.noise > 0.35 && rim > 0.5) alpha *= 1.29;
      alpha = clamp(alpha, 0.04, 0.97);
      const b = Math.min(10, Math.floor(alpha * 11));
      const size = p.small ? (rim > 0.68 ? 1.02 : 0.83) : (rim > 0.68 ? 1.65 : 1.18);
      const hue = nHues === 1 ? 0 : Math.min(6, Math.floor((Math.atan2(yy, xx) / TAU + 1) % 1 * 7));
      buckets[hue * 11 + b].push([cx + xx * R, cy + yy * R, size]);
    }
    context.save();
    for (let b = 0; b < buckets.length; b++) {
      if (!buckets[b].length) continue;
      context.beginPath();
      const alpha = (b % 11 + 0.55) / 11 * 0.88;
      context.fillStyle = nHues === 1 ? `rgba(241,246,255,${alpha})`
        : rgba(mix(MONO.particle, look.tints[Math.floor(b / 11)], look.amount), alpha);
      for (const p of buckets[b]) {
        context.moveTo(p[0] + p[2], p[1]);
        context.arc(p[0], p[1], p[2], 0, TAU);
      }
      context.fill();
    }
    context.restore();

    // Overlapping hairline filaments make the particle rim feel turbulent and organic.
    context.save();
    for (let strand = 0; strand < 6; strand++) {
      context.beginPath();
      const base = 0.952 + strand * 0.014;
      for (let i = 0; i <= 400; i++) {
        const a = i / 400 * TAU;
        const shape = Math.sin(a * 7.0 + strand * 2.1) * 0.027
          + Math.sin(a * 16.0 - strand * 1.3 + frame * (strand % 2 ? 0.21 : -0.16)) * 0.012
          + Math.sin(a * 35.0 + strand * 5.0) * 0.004;
        const v = R * (base + shape);
        const x = cx + v * Math.cos(a);
        const y = cy + v * Math.sin(a);
        if (!i) context.moveTo(x, y); else context.lineTo(x, y);
      }
      context.strokeStyle = rgba(mix(MONO.strand, look.tints[STRAND_TINT[strand]], look.amount),
        (strand % 2 ? 0.065 : 0.035) * (1 + 0.23 * look.amount));
      context.lineWidth = 0.8;
      context.stroke();
    }
    context.restore();

    // Escaping dust: the circle's outer boundary must never be a perfect geometric line.
    context.save();
    const dustBuckets = Array.from({ length: 5 }, () => []);
    for (const p of aura) {
      const a = p.a + (reduced ? 0 : 0.006 * Math.sin(frame * 0.5 + p.phase));
      const noise = 0.014 * Math.sin(a * 13 + frame * 0.24)
        + 0.018 * Math.sin(a * 6 - frame * 0.13 + p.phase);
      const dist = R * (p.d + noise);
      const alpha = clamp((1.2 - Math.abs(p.d - 0.995) * 6) * (0.08 + p.bright * 0.54) * cfg.glow, 0.012, 0.78);
      const b = Math.min(4, Math.floor(alpha * 5));
      dustBuckets[b].push([cx + dist * Math.cos(a), cy + dist * Math.sin(a), p.size]);
    }
    for (let b = 0; b < 5; b++) {
      context.fillStyle = rgba(mix(MONO.dust, look.tints[DUST_TINT[b]], look.amount), 0.11 + b * 0.155);
      context.beginPath();
      for (const p of dustBuckets[b]) {
        const sz = p[2] > 0.975 ? 1.9 : p[2] > 0.65 ? 1.0 : 0.58;
        context.moveTo(p[0] + sz, p[1]);
        context.arc(p[0], p[1], sz, 0, TAU);
      }
      context.fill();
    }
    context.restore();

    const since = seconds - pulseTime;
    if (!reduced && since > 0 && since < 1.7) {
      const f = since / 1.7;
      const v = R * (0.7 + 0.5 * f);
      context.beginPath();
      context.arc(cx, cy, v, 0, TAU);
      context.lineWidth = 1.0 + 0.5 * (1 - f);
      context.strokeStyle = rgba(mix(MONO.ring, look.tints[0], look.amount), 0.24 * Math.pow(1 - f, 2));
      context.stroke();
    }
  }

  function loop(ms) {
    raf = 0;
    if (stopped || document.hidden || motionQuery.matches) return;
    // 30 FPS is enough for slow fluid motion, dramatically cheaper than 60 FPS.
    if (ms - lastRender >= 32.5) {
      draw(ms);
      lastRender = ms;
    }
    raf = window.requestAnimationFrame(loop);
  }
  function request() {
    if (stopped || document.hidden || motionQuery.matches || raf) return;
    raf = window.requestAnimationFrame(loop);
  }
  function updateMotion() {
    if (motionQuery.matches) {
      window.cancelAnimationFrame(raf);
      raf = 0;
      draw(0);
    } else request();
  }
  function setState(next) {
    if (!Object.hasOwn(MODES, next)) return false;
    if (mode !== next) pulseTime = now * 0.001;
    mode = next;
    if (motionQuery.matches || stopped) draw(now);
    return true;
  }
  function setActivity(value) {
    if (!Number.isFinite(value)) return false;
    activity = clamp(value, 0, 1);
    if (motionQuery.matches) draw(0);
    return true;
  }

  function setPalette(value) {
    const next = canonical(value);
    if (typeof next !== 'string' || !PALETTES.includes(next)) return false;
    palette = next;
    if (motionQuery.matches || stopped) draw(now);
    return true;
  }

  function pulse() {
    pulseTime = now * 0.001;
    if (motionQuery.matches || stopped) draw(now);
    return true;
  }

  window.PetronBrain = Object.freeze({
    setState, setActivity, pulse, setPalette,
    modes: Object.freeze(Object.keys(MODES)),
    palettes: PALETTES,
    getPalette: () => palette,
    getState: () => Object.freeze({ mode, activity, palette, reducedMotion: motionQuery.matches }),
    pause: () => { stopped = true; window.cancelAnimationFrame(raf); raf = 0; },
    resume: () => { stopped = false; request(); },
  });
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { window.cancelAnimationFrame(raf); raf = 0; } else request(); });
  window.addEventListener('pointermove', (e) => {
    pointerX = clamp((e.clientX / width - 0.5) * 2, -1, 1);
    pointerY = clamp((e.clientY / height - 0.5) * 2, -1, 1);
  }, { passive: true });
  window.addEventListener('pointerleave', () => { pointerX = 0; pointerY = 0; }, { passive: true });
  window.addEventListener('pointerdown', () => { pulseTime = now * 0.001; }, { passive: true });
  // Keyboard parity with touch: Enter/Space on the focused Brain gives the same discreet pulse.
  canvas.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pulse(); }
  });
  if (motionQuery.addEventListener) motionQuery.addEventListener('change', updateMotion);
  resize();
  request();
})();
