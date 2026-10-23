/**
 * PETRON Brain V0.1
 * A self-contained, CPU-friendly monochrome particle sculpture.
 * No dependencies, remote requests, sensors, model calls or visible controls.
 *
 * Integration API:
 *   window.PetronBrain.setState('idle'|'thinking'|'working'|'success'|'error')
 *   window.PetronBrain.setActivity(0..1)
 *   window.PetronBrain.pause(); window.PetronBrain.resume();
 *   window.PetronBrain.getState()
 */
(() => {
  'use strict';

  const canvas = document.getElementById('brain');
  if (!canvas) return;
  const context = canvas.getContext('2d', { alpha: true, desynchronized: true });
  if (!context) return;

  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  const TAU = Math.PI * 2;
  const MODES = Object.freeze({
    idle: { speed: 0.18, shimmer: 0.10, breathe: 0.010, glow: 1 },
    thinking: { speed: 0.43, shimmer: 0.23, breathe: 0.017, glow: 1.13 },
    working: { speed: 0.58, shimmer: 0.32, breathe: 0.025, glow: 1.16 },
    success: { speed: 0.27, shimmer: 0.14, breathe: 0.020, glow: 1.24 },
    error: { speed: 0.12, shimmer: 0.06, breathe: 0.006, glow: 0.78 },
  });

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
    const cfg = MODES[mode];
    const energy = clamp(activity, 0, 1);
    const reduced = motionQuery.matches;
    const t = reduced ? 0 : seconds * cfg.speed;
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
    const buckets = Array.from({ length: 11 }, () => []);
    const frame = reduced ? 0 : t;
    const flutter = reduced ? 0 : cfg.shimmer;
    const spin = (reduced ? 0 : seconds * 0.06);
    const spinS = Math.sin(spin), spinC = Math.cos(spin);
    for (let i = 0; i < cloud.length; i++) {
      const p = cloud[i];
      const flow = (reduced ? 0 : 0.012 * Math.sin(frame * 0.9 + p.theta * 9 + p.phi * 7));
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
      buckets[b].push([cx + xx * R, cy + yy * R, size]);
    }
    context.save();
    for (let b = 0; b < buckets.length; b++) {
      if (!buckets[b].length) continue;
      context.beginPath();
      context.fillStyle = `rgba(241,246,255,${(b + 0.55) / 11 * 0.88})`;
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
      context.strokeStyle = `rgba(230,238,248,${strand % 2 ? 0.065 : 0.035})`;
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
      context.fillStyle = `rgba(250,251,255,${0.11 + b * 0.155})`;
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
      context.strokeStyle = `rgba(250,250,255,${0.24 * Math.pow(1 - f, 2)})`;
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

  window.PetronBrain = Object.freeze({
    setState, setActivity,
    getState: () => Object.freeze({ mode, activity, reducedMotion: motionQuery.matches }),
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
  if (motionQuery.addEventListener) motionQuery.addEventListener('change', updateMotion);
  resize();
  request();
})();
