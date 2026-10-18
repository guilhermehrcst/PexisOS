/**
 * A small, dependency-free 3D renderer for the Lab 001 fan, drawn as SVG.
 *
 * Geometry is modelled in millimetre-like local units (a 240 x 240 x 50 frame), rotated
 * by a camera (yaw, pitch), projected with a pinhole perspective, shaded by surface
 * normal against one key light, back-face culled and painted far-to-near. Everything
 * here is pure, so the same camera always produces the same scene.
 */

export type Vec3 = readonly [number, number, number];
export type Vec2 = readonly [number, number];

export type FanPart = "frame" | "bore" | "blades" | "hub" | "struts";
export type HotspotId = "motor" | "blades" | "rpmSensor" | "vibration" | "powerInput";

export interface Camera {
  /** Rotation about the vertical axis, radians. Positive turns the right side away. */
  readonly yaw: number;
  /** Rotation about the horizontal axis, radians. Positive tilts the top away. */
  readonly pitch: number;
  /** Uniform zoom factor, 1 = fits the viewport. */
  readonly zoom: number;
}

export interface Viewport {
  readonly width: number;
  readonly height: number;
}

export interface Shape {
  readonly key: string;
  readonly part: FanPart;
  /** SVG path data (may contain several sub-paths drawn with even-odd fill). */
  readonly d: string;
  readonly fill: string;
  readonly stroke?: string;
  readonly strokeWidth?: number;
  readonly depth: number;
}

export interface FanScene {
  readonly shapes: readonly Shape[];
  readonly shadow: string;
  readonly anchors: Readonly<Record<HotspotId, Vec2>>;
}

export const DEFAULT_CAMERA: Camera = { yaw: -0.42, pitch: 0.17, zoom: 1 };
export const FRONT_CAMERA: Camera = { yaw: 0, pitch: 0, zoom: 1 };
export const ZOOM_MIN = 0.7;
export const ZOOM_MAX = 1.35;
export const YAW_LIMIT = 1.05;
export const PITCH_LIMIT = 0.6;

const HALF = 120; // frame half size
const DEPTH = 50; // frame depth
const ZF = DEPTH / 2; // front face z
const ZB = -DEPTH / 2; // back face z
const CORNER = 18; // outer corner radius
const BORE = 112; // bore (opening) radius
const HOLE_R = 6.5;
const HOLE_AT = HALF - 15;
const HUB_R = 38;
const BLADES = 7;
const FOCAL = 900; // camera distance in local units

const LIGHT = normalize([-0.45, 0.62, 0.65]);

export function clampCamera(c: Camera): Camera {
  return {
    yaw: clamp(c.yaw, -YAW_LIMIT, YAW_LIMIT),
    pitch: clamp(c.pitch, -PITCH_LIMIT, PITCH_LIMIT),
    zoom: clamp(c.zoom, ZOOM_MIN, ZOOM_MAX),
  };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, Number.isFinite(v) ? v : 0));
}

function normalize(v: Vec3): Vec3 {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

function sub(a: Vec3, b: Vec3): Vec3 {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** Rotate a local point into camera space (yaw about Y, then pitch about X). */
export function rotate(p: Vec3, c: Camera): Vec3 {
  const cy = Math.cos(c.yaw);
  const sy = Math.sin(c.yaw);
  const x1 = p[0] * cy + p[2] * sy;
  const z1 = -p[0] * sy + p[2] * cy;
  const cp = Math.cos(c.pitch);
  const sp = Math.sin(c.pitch);
  const y2 = p[1] * cp - z1 * sp;
  const z2 = p[1] * sp + z1 * cp;
  return [x1, y2, z2];
}

/** Fit scale: the frame fills ~76% of the shorter viewport side at zoom 1. */
function baseScale(v: Viewport): number {
  return (Math.min(v.width, v.height) * 0.82) / (HALF * 2 * 1.08);
}

/** Project a camera-space point to viewport pixels (y down). */
export function projectCam(p: Vec3, c: Camera, v: Viewport): Vec2 {
  const k = (FOCAL / (FOCAL - p[2])) * baseScale(v) * c.zoom;
  return [v.width / 2 + p[0] * k, v.height / 2 - p[1] * k];
}

export function project(p: Vec3, c: Camera, v: Viewport): Vec2 {
  return projectCam(rotate(p, c), c, v);
}

/* ------------------------------------------------------------------ colours */

type RGB = readonly [number, number, number];
const FRAME_RGB: RGB = [44, 48, 56];
const BLADE_RGB: RGB = [36, 39, 46];
const HUB_RGB: RGB = [50, 54, 63];
const STRUT_RGB: RGB = [30, 32, 38];

function shade(base: RGB, normal: Vec3, ambient = 0.42, gain = 0.78): string {
  const lambert = Math.max(0, dot(normal, LIGHT));
  // Light falls off into a slightly cool, blue-grey highlight like anodised plastic.
  const k = ambient + gain * lambert;
  const r = Math.round(Math.min(255, base[0] * k + 18 * lambert));
  const g = Math.round(Math.min(255, base[1] * k + 22 * lambert));
  const b = Math.round(Math.min(255, base[2] * k + 30 * lambert));
  return `rgb(${r},${g},${b})`;
}

/* ----------------------------------------------------------------- geometry */

function roundedSquare(z: number, half = HALF, r = CORNER, seg = 6): Vec3[] {
  const pts: Vec3[] = [];
  const corners: readonly [number, number, number][] = [
    [half - r, half - r, 0],
    [-(half - r), half - r, Math.PI / 2],
    [-(half - r), -(half - r), Math.PI],
    [half - r, -(half - r), (3 * Math.PI) / 2],
  ];
  for (const [cx, cy, a0] of corners) {
    for (let i = 0; i <= seg; i++) {
      const a = a0 + (i / seg) * (Math.PI / 2);
      pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a), z]);
    }
  }
  return pts;
}

function circle(cx: number, cy: number, r: number, z: number, seg: number, phase = 0): Vec3[] {
  const pts: Vec3[] = [];
  for (let i = 0; i < seg; i++) {
    const a = phase + (i / seg) * Math.PI * 2;
    pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a), z]);
  }
  return pts;
}

function pathOf(points: readonly Vec2[], close = true): string {
  if (points.length === 0) return "";
  const [first, ...rest] = points;
  const f = first as Vec2;
  return `M${f[0].toFixed(2)} ${f[1].toFixed(2)}` + rest.map((p) => `L${p[0].toFixed(2)} ${p[1].toFixed(2)}`).join("") + (close ? "Z" : "");
}

function polyNormal(cam: readonly Vec3[]): Vec3 {
  const [a, b, c] = cam;
  if (!a || !b || !c) return [0, 0, 1];
  return normalize(cross(sub(b, a), sub(c, a)));
}

function centroid(pts: readonly Vec3[]): Vec3 {
  let x = 0, y = 0, z = 0;
  for (const p of pts) { x += p[0]; y += p[1]; z += p[2]; }
  const n = pts.length || 1;
  return [x / n, y / n, z / n];
}

/** True when the camera-space polygon faces the viewer (counter-clockwise winding). */
function facesViewer(cam: readonly Vec3[]): boolean {
  const n = polyNormal(cam);
  const c = centroid(cam);
  const toEye: Vec3 = [-c[0], -c[1], FOCAL - c[2]];
  return dot(n, toEye) > 0;
}

/* --------------------------------------------------------------------- blades */

const BLADE_R0 = HUB_R - 1;
const BLADE_R1 = BORE - 7;

function bladeEdge(k: number, u: number, side: "lead" | "trail"): number {
  const theta = (k / BLADES) * Math.PI * 2;
  return side === "lead"
    ? theta + 0.36 + 0.62 * u - 0.06 * Math.sin(Math.PI * u)
    : theta - 0.34 + 0.78 * u + 0.04 * Math.sin(Math.PI * u);
}

/** Blade surface point. w in [0 (trailing), 1 (leading)], u in [0 (root), 1 (tip)]. */
function bladePoint(k: number, u: number, w: number): Vec3 {
  const r = BLADE_R0 + (BLADE_R1 - BLADE_R0) * u;
  const a = bladeEdge(k, u, "trail") + (bladeEdge(k, u, "lead") - bladeEdge(k, u, "trail")) * w;
  // Twist: the leading edge sits forward, the trailing edge back; less pitch at the tip,
  // plus a gentle camber so each column shades differently.
  const pitch = 13 - 5 * u;
  const z = 2 + (w - 0.5) * 2 * pitch - 3.5 * Math.sin(Math.PI * w);
  return [r * Math.cos(a), r * Math.sin(a), z];
}

/* ---------------------------------------------------------------------- scene */

export function buildFanScene(camera: Camera, viewport: Viewport, highlight: FanPart | null = null): FanScene {
  const c = clampCamera(camera);
  const toCam = (p: Vec3) => rotate(p, c);
  const toScreen = (p: Vec3) => projectCam(toCam(p), c, viewport);
  const shapes: Shape[] = [];
  const tint = (part: FanPart, fill: string) => (highlight === part ? mixBlue(fill) : fill);

  const addPoly = (key: string, part: FanPart, pts: readonly Vec3[], base: RGB, opts: { cull?: boolean; ambient?: number; gain?: number; stroke?: string; strokeWidth?: number; bias?: number } = {}) => {
    const cam = pts.map(toCam);
    if (opts.cull !== false && !facesViewer(cam)) return;
    const n = polyNormal(cam);
    const fill = tint(part, shade(base, n, opts.ambient, opts.gain));
    const shape: Shape = {
      key, part, d: pathOf(cam.map((p) => projectCam(p, c, viewport))), fill,
      depth: centroid(cam)[2] + (opts.bias ?? 0),
      ...(opts.stroke ? { stroke: opts.stroke, strokeWidth: opts.strokeWidth ?? 1 } : {}),
    };
    shapes.push(shape);
  };

  // 1. Bore wall (inside of the opening). Inward normals: only the far side is visible.
  const boreSeg = 56;
  for (let i = 0; i < boreSeg; i++) {
    const a0 = (i / boreSeg) * Math.PI * 2;
    const a1 = ((i + 1) / boreSeg) * Math.PI * 2;
    const p = (a: number, z: number): Vec3 => [BORE * Math.cos(a), BORE * Math.sin(a), z];
    // Winding gives an inward (towards the axis) normal: the bore is seen from inside.
    addPoly(`bore-${i}`, "bore", [p(a0, ZF), p(a1, ZF), p(a1, ZB), p(a0, ZB)], FRAME_RGB, { ambient: 0.3, gain: 0.5, bias: -400 });
  }

  // 2. Rear motor mount and struts, seen through the blades.
  const zs = ZB + 6;
  addPoly("mount", "struts", circle(0, 0, HUB_R + 6, zs, 36), STRUT_RGB, { cull: false, bias: -300 });
  for (let s = 0; s < 4; s++) {
    const a = Math.PI / 4 + (s * Math.PI) / 2;
    const ca = Math.cos(a), sa = Math.sin(a);
    const w = 5;
    const r0 = HUB_R + 4, r1 = BORE + 2;
    const pt = (r: number, off: number): Vec3 => [r * ca - off * sa, r * sa + off * ca, zs];
    addPoly(`strut-${s}`, "struts", [pt(r0, -w), pt(r1, -w), pt(r1, w), pt(r0, w)], STRUT_RGB, { cull: false, bias: -300 });
  }

  // 3. Blades, each split into columns across its width so the twist reads as curvature.
  const cols = 4;
  const rows = 6;
  const bladeShapes: Shape[] = [];
  for (let k = 0; k < BLADES; k++) {
    const bladeDepth: number[] = [];
    const start = shapes.length;
    for (let j = 0; j < cols; j++) {
      const w0 = j / cols, w1 = (j + 1) / cols;
      const outline: Vec3[] = [];
      for (let i = 0; i <= rows; i++) outline.push(bladePoint(k, i / rows, w0));
      for (let i = rows; i >= 0; i--) outline.push(bladePoint(k, i / rows, w1));
      const cam = outline.map(toCam);
      let n = polyNormal([cam[0] as Vec3, cam[rows] as Vec3, cam[rows + 2] as Vec3]);
      if (n[2] < 0) n = [-n[0], -n[1], -n[2]];
      bladeDepth.push(centroid(cam)[2]);
      shapes.push({
        key: `blade-${k}-${j}`, part: "blades",
        d: pathOf(cam.map((p) => projectCam(p, c, viewport))),
        fill: tint("blades", shade(BLADE_RGB, n, 0.4, 0.78)),
        stroke: tint("blades", shade(BLADE_RGB, n, 0.4, 0.78)), strokeWidth: 0.6,
        depth: 0,
      });
    }
    // Edge highlight along the leading edge and tip.
    const edge: Vec3[] = [];
    for (let i = 0; i <= rows; i++) edge.push(bladePoint(k, i / rows, 1));
    for (let j = cols; j >= 0; j--) edge.push(bladePoint(k, 1, j / cols));
    shapes.push({
      key: `blade-edge-${k}`, part: "blades",
      d: pathOf(edge.map(toScreen), false), fill: "none",
      stroke: highlight === "blades" ? "rgba(20,120,255,0.9)" : "rgba(160,172,190,0.55)", strokeWidth: 1.1, depth: 0,
    });
    const avg = bladeDepth.reduce((a, b) => a + b, 0) / bladeDepth.length;
    for (let i = start; i < shapes.length; i++) {
      const s = shapes[i] as Shape;
      bladeShapes.push({ ...s, depth: avg + (s.key.startsWith("blade-edge") ? 0.01 : 0) });
    }
    shapes.length = start;
  }
  bladeShapes.sort((a, b) => a.depth - b.depth);

  // 4. Hub: cylinder side, front cap and label disc.
  const hubBack = -6, hubFront = 16, hubSeg = 40;
  const hubShapes: Shape[] = [];
  const pushHub = (s: Shape | undefined) => { if (s) hubShapes.push(s); };
  for (let i = 0; i < hubSeg; i++) {
    const a0 = (i / hubSeg) * Math.PI * 2, a1 = ((i + 1) / hubSeg) * Math.PI * 2;
    const p = (a: number, z: number): Vec3 => [HUB_R * Math.cos(a), HUB_R * Math.sin(a), z];
    const quad = [p(a0, hubBack), p(a1, hubBack), p(a1, hubFront), p(a0, hubFront)];
    const cam = quad.map(toCam);
    if (!facesViewer(cam)) continue;
    pushHub({
      key: `hub-side-${i}`, part: "hub", d: pathOf(cam.map((q) => projectCam(q, c, viewport))),
      fill: tint("hub", shade(HUB_RGB, polyNormal(cam), 0.4, 0.7)), depth: 0,
    });
  }
  const capCam = circle(0, 0, HUB_R, hubFront, hubSeg).map(toCam);
  if (facesViewer(capCam)) {
    pushHub({ key: "hub-cap", part: "hub", d: pathOf(capCam.map((q) => projectCam(q, c, viewport))), fill: tint("hub", shade(HUB_RGB, polyNormal(capCam), 0.46, 0.62)), depth: 0 });
    pushHub({ key: "hub-label", part: "hub", d: pathOf(circle(0, 0, 25, hubFront + 0.2, hubSeg).map(toScreen)), fill: tint("hub", "rgb(28,31,37)"), stroke: highlight === "hub" ? "rgba(20,120,255,0.9)" : "rgba(120,132,150,0.45)", strokeWidth: 1, depth: 0 });
    pushHub({ key: "hub-ring", part: "hub", d: pathOf(circle(0, 0, HUB_R - 3, hubFront + 0.1, hubSeg).map(toScreen)), fill: "none", stroke: "rgba(150,162,180,0.35)", strokeWidth: 1, depth: 0 });
  }

  // 5. Outer frame walls (only the ones turned towards the viewer).
  const front = roundedSquare(ZF);
  const back = roundedSquare(ZB);
  const wallShapes: Shape[] = [];
  for (let i = 0; i < front.length; i++) {
    const j = (i + 1) % front.length;
    // front i -> back i -> back j -> front j gives an outward normal for a CCW outline.
    const quad = [front[i] as Vec3, back[i] as Vec3, back[j] as Vec3, front[j] as Vec3];
    const cam = quad.map(toCam);
    if (!facesViewer(cam)) continue;
    wallShapes.push({
      key: `wall-${i}`, part: "frame", d: pathOf(cam.map((q) => projectCam(q, c, viewport))),
      fill: tint("frame", shade(FRAME_RGB, polyNormal(cam), 0.36, 0.7)),
      stroke: tint("frame", shade(FRAME_RGB, polyNormal(cam), 0.36, 0.7)), strokeWidth: 0.6, depth: 0,
    });
  }

  // 6. Front face: rounded square minus the bore and four mounting holes (even-odd).
  const faceCam = front.map(toCam);
  const faceVisible = facesViewer(faceCam);
  const faceShapes: Shape[] = [];
  if (faceVisible) {
    const holes = [
      circle(0, 0, BORE, ZF, 72),
      ...[[1, 1], [-1, 1], [-1, -1], [1, -1]].map(([sx, sy]) => circle((sx as number) * HOLE_AT, (sy as number) * HOLE_AT, HOLE_R, ZF, 18)),
    ];
    const d = [front, ...holes].map((ring) => pathOf(ring.map(toScreen))).join("");
    faceShapes.push({ key: "face", part: "frame", d, fill: tint("frame", shade(FRAME_RGB, polyNormal(faceCam), 0.5, 0.55)), depth: 0 });
    // Bevel highlight on the outer edge and the bore lip.
    faceShapes.push({ key: "face-edge", part: "frame", d: pathOf(front.map(toScreen)), fill: "none", stroke: highlight === "frame" ? "rgba(20,120,255,0.95)" : "rgba(170,182,200,0.55)", strokeWidth: 1.2, depth: 0 });
    faceShapes.push({ key: "bore-lip", part: "frame", d: pathOf(circle(0, 0, BORE + 3, ZF + 0.1, 72).map(toScreen)), fill: "none", stroke: "rgba(120,132,150,0.4)", strokeWidth: 1, depth: 0 });
  }

  // Paint order: behind-the-opening parts, blades, hub, frame. When the face is turned
  // away (strong yaw) the frame is drawn first so the opening reads correctly.
  const behind = shapes.sort((a, b) => a.depth - b.depth);
  const ordered = faceVisible
    ? [...behind, ...bladeShapes, ...hubShapes, ...wallShapes, ...faceShapes]
    : [...wallShapes, ...behind, ...bladeShapes, ...hubShapes];

  // Soft shadow: the back outline, pushed down and right, blurred by the component.
  const shadow = pathOf(back.map((p) => {
    const s = toScreen(p);
    return [s[0] + 26 * c.zoom, s[1] + 38 * c.zoom] as Vec2;
  }));

  const anchors: Record<HotspotId, Vec2> = {
    motor: toScreen([0, 0, hubFront]),
    rpmSensor: toScreen([-HUB_R + 6, HUB_R - 26, hubFront]),
    blades: toScreen(bladePoint(0, 0.68, 0.6)),
    vibration: toScreen(bladePoint(4, 0.5, 0.5)),
    // Where a fan's power lead leaves the motor: along the lower-right strut, at the bore lip.
    powerInput: toScreen([(BORE + 4) * Math.cos(-0.62), (BORE + 4) * Math.sin(-0.62), ZF]),
  };

  return { shapes: ordered, shadow, anchors };
}

function mixBlue(fill: string): string {
  const m = /rgb\((\d+),(\d+),(\d+)\)/.exec(fill);
  if (!m) return fill;
  const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const t = 0.42;
  return `rgb(${Math.round(r + (20 - r) * t)},${Math.round(g + (120 - g) * t)},${Math.round(b + (255 - b) * t)})`;
}

export const HOTSPOT_PART: Readonly<Record<HotspotId, FanPart>> = {
  motor: "hub",
  rpmSensor: "hub",
  blades: "blades",
  vibration: "frame",
  powerInput: "struts",
};
