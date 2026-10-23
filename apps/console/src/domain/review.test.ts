import { describe, expect, it } from "vitest";
import { assess, assessMetric, formatPercent } from "./evidence";
import { AS_OF, RPM_BASELINE, WINDOW_START, buildScenario } from "./scenario";
import type { MetricId, MetricSeries, Sample, Scenario } from "./types";

/** Regressions for the independent review of 2f5abbb (F1-F3, F6, F7). */

const ticks = (from: number, to: number, v: (t: number) => number | null): Sample[] => {
  const out: Sample[] = [];
  for (let t = from; t <= to; t += 5000) out.push({ t, v: v(t) });
  return out;
};
const withSeries = (base: Scenario, id: MetricId, patch: Partial<MetricSeries>): Scenario => ({
  ...base,
  series: { ...base.series, [id]: { ...base.series[id], ...patch } },
});

describe("F1: any sustained deviation reaches the insight and the subsystems", () => {
  it("temperature out of range is not reported as all nominal", () => {
    const s = withSeries(buildScenario("nominal"), "temperature", { samples: ticks(WINDOW_START, AS_OF, () => 75) });
    const a = assess(s);
    expect(a.metrics.temperature.state).toBe("attention");
    expect(a.health.nominal).toBeLessThan(5);
    expect(a.insight.state).not.toBe("nominal");
    expect(a.insight.observation).not.toMatch(/stay within their expected ranges/);
  });
  it("voltage out of range is not reported as all nominal", () => {
    const s = withSeries(buildScenario("nominal"), "voltage", { samples: ticks(WINDOW_START, AS_OF, () => 10.2) });
    const a = assess(s);
    expect(a.insight.state).not.toBe("nominal");
    expect(a.insight.observation).not.toMatch(/stay within their expected ranges/);
  });
});

describe("F2: insight sentences are derived, not fixed", () => {
  it("does not claim voltage is in range when it is not", () => {
    const s = withSeries(buildScenario("rpm-deviation"), "voltage", { samples: ticks(WINDOW_START, AS_OF, () => 10.2) });
    expect(assess(s).insight.evidence).not.toMatch(/voltage and current in range/);
  });
  it("does not claim stale RPM was below the baseline when it was nominal", () => {
    const s = withSeries(buildScenario("nominal"), "rpm", { samples: ticks(WINDOW_START, AS_OF - 90_000, () => RPM_BASELINE) });
    const a = assess(s);
    expect(a.insight.state).toBe("stale");
    expect(a.insight.evidence).not.toMatch(/below the baseline/);
  });
  it("uses the series baseline, not a literal", () => {
    const base = buildScenario("rpm-deviation");
    const s = withSeries(base, "rpm", { baseline: 2000, samples: ticks(WINDOW_START, AS_OF, (t) => (t >= AS_OF - 200_000 ? 1500 : 2000)) });
    const text = assess(s).insight.lines.map((l) => l.text).join(" ");
    expect(text).toMatch(/2000 RPM synthetic baseline/);
    expect(text).not.toMatch(/1800/);
  });
});

describe("F3: a data gap breaks a sustained run", () => {
  it("two deviated samples around a gap are not a sustained deviation", () => {
    const rpm = buildScenario("rpm-deviation").series.rpm;
    const samples: Sample[] = [
      ...ticks(WINDOW_START, AS_OF - 130_000, () => 1800),
      ...ticks(AS_OF - 125_000 + 5000, AS_OF - 5000, () => null),
      { t: AS_OF - 125_000, v: 1400 },
      { t: AS_OF, v: 1400 },
    ];
    const m = assessMetric({ ...rpm, samples }, AS_OF, 5000);
    expect(m.state).not.toBe("attention");
  });
});

describe("F6: percent formatting", () => {
  it("rounds symmetrically and never prints NaN", () => {
    expect(formatPercent(0.025)).toBe("+3%");
    expect(formatPercent(-0.025)).toBe("−3%");
    expect(formatPercent(Number.NaN)).toBe("—");
  });
  it("a zero baseline does not produce a deviation", () => {
    const rpm = buildScenario("nominal").series.rpm;
    expect(assessMetric({ ...rpm, baseline: 0 }, AS_OF, 5000).deviation).toBeNull();
  });
});

describe("F7: stale power input is not shown as a current value", () => {
  it("shows the state, not the stale voltage", () => {
    const s = withSeries(buildScenario("nominal"), "voltage", { samples: ticks(WINDOW_START, AS_OF - 90_000, () => 12.1) });
    const power = assess(s).subsystems.find((x) => x.id === "powerInput");
    expect(power?.state).toBe("stale");
    expect(power?.status).not.toMatch(/V$/);
  });
});
