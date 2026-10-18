import { describe, expect, it } from "vitest";
import {
  DEFAULT_CAMERA, FRONT_CAMERA, ZOOM_MAX, ZOOM_MIN, buildFanScene, clampCamera, project, rotate,
} from "./fan3d";

const VIEW = { width: 640, height: 420 };

describe("fan renderer", () => {
  it("is deterministic", () => {
    expect(buildFanScene(DEFAULT_CAMERA, VIEW)).toEqual(buildFanScene(DEFAULT_CAMERA, VIEW));
  });

  it("projects the origin to the viewport centre", () => {
    expect(project([0, 0, 0], DEFAULT_CAMERA, VIEW)).toEqual([320, 210]);
  });

  it("rotation preserves length", () => {
    const p = rotate([30, -40, 12], { yaw: 0.7, pitch: -0.3, zoom: 1 });
    expect(Math.hypot(...p)).toBeCloseTo(Math.hypot(30, -40, 12), 9);
  });

  it("front view shows the face and no side walls", () => {
    const keys = buildFanScene(FRONT_CAMERA, VIEW).shapes.map((s) => s.key);
    expect(keys).toContain("face");
    expect(keys.some((k) => k.startsWith("wall-"))).toBe(false);
  });

  it("a turned view reveals side walls and the far bore wall, never the near one", () => {
    const shapes = buildFanScene(DEFAULT_CAMERA, VIEW).shapes;
    const walls = shapes.filter((s) => s.key.startsWith("wall-")).length;
    const bore = shapes.filter((s) => s.key.startsWith("bore-")).length;
    expect(walls).toBeGreaterThan(0);
    expect(bore).toBeGreaterThan(0);
    expect(bore).toBeLessThan(56); // back-face culling removed the near half
  });

  it("draws all seven blades", () => {
    const blades = new Set(buildFanScene(DEFAULT_CAMERA, VIEW).shapes.filter((s) => s.key.startsWith("blade-edge-")).map((s) => s.key));
    expect(blades.size).toBe(7);
  });

  it("keeps every hotspot anchor inside the viewport", () => {
    for (const cam of [DEFAULT_CAMERA, FRONT_CAMERA, { yaw: 1, pitch: -0.5, zoom: ZOOM_MAX }]) {
      for (const [x, y] of Object.values(buildFanScene(cam, VIEW).anchors)) {
        expect(x).toBeGreaterThan(0);
        expect(x).toBeLessThan(VIEW.width);
        expect(y).toBeGreaterThan(0);
        expect(y).toBeLessThan(VIEW.height);
      }
    }
  });

  it("anchors follow the camera", () => {
    const a = buildFanScene(DEFAULT_CAMERA, VIEW).anchors.powerInput;
    const b = buildFanScene({ ...DEFAULT_CAMERA, yaw: 0.4 }, VIEW).anchors.powerInput;
    expect(a).not.toEqual(b);
  });

  it("clamps camera input, including non-finite values", () => {
    expect(clampCamera({ yaw: 9, pitch: -9, zoom: 99 })).toEqual({ yaw: 1.05, pitch: -0.6, zoom: ZOOM_MAX });
    expect(clampCamera({ yaw: Number.NaN, pitch: Number.POSITIVE_INFINITY, zoom: 0 })).toEqual({ yaw: 0, pitch: 0, zoom: ZOOM_MIN });
  });

  it("tints only the highlighted part", () => {
    const plain = buildFanScene(DEFAULT_CAMERA, VIEW);
    const lit = buildFanScene(DEFAULT_CAMERA, VIEW, "blades");
    const fills = (s: typeof plain, part: string) => s.shapes.filter((x) => x.part === part).map((x) => x.fill);
    expect(fills(lit, "blades")).not.toEqual(fills(plain, "blades"));
    expect(fills(lit, "hub")).toEqual(fills(plain, "hub"));
  });
});
