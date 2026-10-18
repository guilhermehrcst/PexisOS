import { describe, expect, it } from "vitest";
import { niceStep, yScale } from "./chartScale";

describe("chart scale", () => {
  it("picks nice steps", () => {
    expect(niceStep(2100, 4)).toBe(600);
    expect(niceStep(1, 4)).toBe(0.25);
    expect(niceStep(0, 4)).toBe(1);
    expect(niceStep(Number.NaN, 4)).toBe(1);
  });
  it("includes the baseline and zero for RPM, like the reference axis", () => {
    const s = yScale([1420, 1810, 1790], 1800);
    expect(s.ticks).toEqual([0, 600, 1200, 1800, 2400]);
  });
  it("ignores non-finite values and survives flat or empty input", () => {
    expect(yScale([Number.NaN, 5, Number.POSITIVE_INFINITY]).ticks.length).toBeGreaterThan(1);
    expect(yScale([]).ticks).toEqual([0, 1]);
    const flat = yScale([12.1, 12.1]);
    expect(flat.max).toBeGreaterThan(flat.min);
  });
});
