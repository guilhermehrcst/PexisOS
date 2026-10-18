import { Activity, BarChart3, Cog, Fan, Minus, Plus, Zap } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import type { SubsystemAssessment } from "../domain/evidence";
import { useElementSize } from "../hooks/useElementSize";
import {
  DEFAULT_CAMERA, FRONT_CAMERA, HOTSPOT_PART, ZOOM_MAX, ZOOM_MIN, buildFanScene, clampCamera,
} from "../viz/fan3d";
import type { Camera, HotspotId } from "../viz/fan3d";
import { ComponentHotspot } from "./ComponentHotspot";
import type { HotspotLayout } from "./ComponentHotspot";
import "./DeviceVisualization.css";

/** Where each hotspot label sits, as a fraction of the visualization box (matches the V1 reference). */
export const HOTSPOT_LAYOUT: Readonly<Record<HotspotId, HotspotLayout>> = {
  motor: { x: 0.205, y: 0.155, side: "left", icon: Cog },
  rpmSensor: { x: 0.185, y: 0.455, side: "left", icon: BarChart3 },
  vibration: { x: 0.235, y: 0.745, side: "left", icon: Activity },
  blades: { x: 0.78, y: 0.27, side: "right", icon: Fan },
  powerInput: { x: 0.78, y: 0.6, side: "right", icon: Zap },
};

export const HOTSPOT_ORDER: readonly HotspotId[] = ["motor", "rpmSensor", "vibration", "blades", "powerInput"];

const ZOOM_STEP = 0.1;
const ROTATE_STEP = 0.12;

interface Props {
  readonly subsystems: readonly SubsystemAssessment[];
  readonly selected: HotspotId | null;
  readonly onSelect: (id: HotspotId | null) => void;
}

export function DeviceVisualization({ subsystems, selected, onSelect }: Props) {
  const [box, size] = useElementSize<HTMLDivElement>({ width: 680, height: 420 });
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA);
  const [perspective, setPerspective] = useState(true);
  const drag = useRef<{ x: number; y: number; cam: Camera; moved: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);
  const filterId = `fan-shadow-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;

  const scene = useMemo(
    () => buildFanScene(camera, size, selected ? HOTSPOT_PART[selected] : null),
    [camera, size, selected],
  );
  const byId = useMemo(() => new Map(subsystems.map((s) => [s.id, s])), [subsystems]);

  const zoomBy = (delta: number) => setCamera((c) => clampCamera({ ...c, zoom: c.zoom + delta }));
  const rotateBy = (dYaw: number, dPitch: number) => {
    setPerspective(true);
    setCamera((c) => clampCamera({ ...c, yaw: c.yaw + dYaw, pitch: c.pitch + dPitch }));
  };
  const togglePerspective = () => {
    setPerspective((p) => {
      const next = !p;
      setCamera((c) => ({ ...(next ? DEFAULT_CAMERA : FRONT_CAMERA), zoom: c.zoom }));
      return next;
    });
  };

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    drag.current = { x: e.clientX, y: e.clientY, cam: camera, moved: false };
    setDragging(true);
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 3) return;
    d.moved = true;
    setPerspective(true);
    setCamera(clampCamera({ ...d.cam, yaw: d.cam.yaw + dx * 0.008, pitch: d.cam.pitch + dy * 0.006 }));
  };
  const onPointerUp = () => { drag.current = null; setDragging(false); };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, () => void> = {
      ArrowLeft: () => rotateBy(-ROTATE_STEP, 0),
      ArrowRight: () => rotateBy(ROTATE_STEP, 0),
      ArrowUp: () => rotateBy(0, -ROTATE_STEP),
      ArrowDown: () => rotateBy(0, ROTATE_STEP),
      "+": () => zoomBy(ZOOM_STEP),
      "=": () => zoomBy(ZOOM_STEP),
      "-": () => zoomBy(-ZOOM_STEP),
      "0": () => { setPerspective(true); setCamera(DEFAULT_CAMERA); },
    };
    const action = keys[e.key];
    if (action) { e.preventDefault(); action(); }
  };

  return (
    <section className="viz" aria-label="Lab 001 fan model">
      <div className="viz__stage" ref={box}>
        <div
          className="viz__canvas"
          tabIndex={0}
          role="group"
          aria-roledescription="3D model"
          aria-label="Smart fan model. Drag or use arrow keys to rotate, plus and minus to zoom, 0 to reset."
          onKeyDown={onKeyDown}
        >
          <svg
            width={size.width}
            height={size.height}
            viewBox={`0 0 ${size.width} ${size.height}`}
            className={`viz__svg${dragging ? " is-dragging" : ""}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            aria-hidden="true"
          >
            <defs>
              <filter id={filterId} x="-30%" y="-30%" width="160%" height="160%">
                <feGaussianBlur stdDeviation="22" />
              </filter>
            </defs>
            <path d={scene.shadow} fill="rgba(30, 41, 59, 0.26)" filter={`url(#${filterId})`} />
            {scene.shapes.map((s) => (
              <path
                key={s.key}
                d={s.d}
                fill={s.fill}
                fillRule="evenodd"
                stroke={s.stroke ?? "none"}
                strokeWidth={s.strokeWidth ?? 0}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ))}
            {HOTSPOT_ORDER.map((id) => {
              const layout = HOTSPOT_LAYOUT[id];
              const [ax, ay] = scene.anchors[id];
              const lx = layout.x * size.width;
              const ly = layout.y * size.height;
              const on = selected === id;
              return (
                <g key={id} className={`viz__link${on ? " is-on" : ""}`}>
                  <path d={`M${lx} ${ly} L${ax} ${ay}`} />
                  <circle cx={ax} cy={ay} r={on ? 8 : 6.5} className="viz__anchor" />
                </g>
              );
            })}
          </svg>
        </div>
        {HOTSPOT_ORDER.map((id) => {
          const sub = byId.get(id);
          if (!sub) return null;
          return (
            <ComponentHotspot
              key={id}
              subsystem={sub}
              layout={HOTSPOT_LAYOUT[id]}
              selected={selected === id}
              onToggle={() => onSelect(selected === id ? null : id)}
            />
          );
        })}
      </div>
      <div className="viz__controls" role="toolbar" aria-label="Model view">
        <button type="button" className="viz__control" onClick={() => zoomBy(ZOOM_STEP)} disabled={camera.zoom >= ZOOM_MAX} aria-label="Zoom in">
          <Plus size={18} strokeWidth={2.2} />
        </button>
        <button type="button" className="viz__control" onClick={() => zoomBy(-ZOOM_STEP)} disabled={camera.zoom <= ZOOM_MIN} aria-label="Zoom out">
          <Minus size={18} strokeWidth={2.2} />
        </button>
        <button
          type="button"
          className="viz__control viz__control--text"
          onClick={togglePerspective}
          aria-pressed={perspective}
          aria-label="3D perspective view"
          title={perspective ? "Switch to front view" : "Switch to 3D view"}
        >
          3D
        </button>
      </div>
    </section>
  );
}
