import { describe, expect, it } from "vitest";
import {
  SUSTAINED_MS, assess, assessMetric, formatClock, formatDuration, formatNumber, formatPercent,
  trailingRunMs, validSamples,
} from "./evidence";
import { buildReport } from "./report";
import { AS_OF, RPM_BASELINE, SCENARIO_IDS, WINDOW_START, buildScenario, isScenarioId } from "./scenario";
import type { MetricSeries, Sample } from "./types";

const rpmSeries = (samples: Sample[], extra: Partial<MetricSeries> = {}): MetricSeries => ({
  id: "rpm", label: "Rotational Speed", shortLabel: "RPM", unit: "RPM", decimals: 0,
  sensorAvailable: true, normalRange: { min: 1600, max: 2000 }, baseline: RPM_BASELINE, samples, ...extra,
});
const ticks = (from: number, to: number, v: (t: number) => number | null): Sample[] => {
  const out: Sample[] = [];
  for (let t = from; t <= to; t += 5000) out.push({ t, v: v(t) });
  return out;
};

describe("synthetic scenarios", () => {
  it("are deterministic", () => {
    for (const id of SCENARIO_IDS) expect(buildScenario(id)).toEqual(buildScenario(id));
  });
  it("are always labelled synthetic and cover 30 minutes", () => {
    for (const id of SCENARIO_IDS) {
      const s = buildScenario(id);
      expect(s.provenance).toBe("synthetic");
      expect(s.asOf - s.windowStart).toBe(30 * 60 * 1000);
    }
  });
  it("validates scenario ids", () => {
    expect(isScenarioId("rpm-deviation")).toBe(true);
    expect(isScenarioId("javascript:alert(1)")).toBe(false);
    expect(isScenarioId(undefined)).toBe(false);
  });
});

describe("default scenario: RPM below baseline", () => {
  const a = assess(buildScenario("rpm-deviation"));

  it("derives every headline number from the series", () => {
    expect(a.metrics.rpm.latest?.value).toBe(1420);
    expect(formatPercent(a.metrics.rpm.deviation ?? 0)).toBe("−21%");
    expect(a.metrics.rpm.sustainedMs).toBe(210_000);
    expect(a.metrics.rpm.state).toBe("attention");
    expect(a.metrics.temperature.latest?.value).toBe(34);
    expect(a.metrics.voltage.latest?.value).toBe(12.1);
    expect(a.metrics.current.latest?.value).toBe(0.18);
    expect(a.metrics.vibration.latest?.value).toBe(0.12);
  });
  it("keeps the other readings nominal", () => {
    for (const id of ["temperature", "voltage", "current", "vibration"] as const) {
      expect(a.metrics[id].state).toBe("nominal");
    }
  });
  it("reports subsystems as a count, with inferred statuses labelled", () => {
    expect(a.health).toEqual({ nominal: 4, total: 5, fraction: 0.8 });
    const byId = Object.fromEntries(a.subsystems.map((s) => [s.id, s]));
    expect(byId.rpmSensor?.state).toBe("attention");
    expect(byId.motor?.basis).toBe("inferred");
    expect(byId.blades?.basis).toBe("inferred");
    expect(byId.blades?.confidence).toBe("low");
    expect(byId.powerInput?.basis).toBe("measured");
  });
  it("produces the evidence insight without claiming a diagnosis or a cause", () => {
    expect(a.insight.observation).toBe("fan_rpm remains below the baseline for 210 s.");
    expect(a.insight.status).toBe("suspected anomaly.");
    expect(a.insight.confidence).toBe("medium");
    expect(a.insight.note).toMatch(/not a diagnosis/);
    const all = JSON.stringify(a.insight);
    expect(all).not.toMatch(/fail(ed|ure)|broken|defect|will fail/i);
  });
  it("counts evidence coverage from real check results", () => {
    expect(a.coverage).toEqual({ evaluated: 5, total: 7, fraction: 5 / 7 });
    expect(a.checks.filter((c) => !c.evaluated).map((c) => c.id)).toEqual(["blade-spectrum", "bearing-trend"]);
  });
});

describe("other scenarios", () => {
  it("nominal: no suspicion, 5 of 5 subsystems", () => {
    const a = assess(buildScenario("nominal"));
    expect(a.metrics.rpm.state).toBe("nominal");
    expect(a.health.nominal).toBe(5);
    expect(a.insight.status).toBe("no deviation detected.");
  });
  it("stale RPM: refuses to describe the present", () => {
    const a = assess(buildScenario("stale-rpm"));
    expect(a.metrics.rpm.state).toBe("stale");
    expect(a.metrics.rpm.latest?.ageMs).toBe(95_000);
    expect(a.insight.state).toBe("stale");
    expect(a.insight.confidence).toBe("none");
    expect(a.checks.find((c) => c.id === "rpm-baseline")?.evaluated).toBe(false);
    expect(a.health.nominal).toBe(4);
  });
  it("vibration unavailable: blades become insufficient, not nominal", () => {
    const a = assess(buildScenario("vibration-unavailable"));
    expect(a.metrics.vibration.state).toBe("unavailable");
    const blades = a.subsystems.find((s) => s.id === "blades");
    expect(blades?.state).toBe("insufficient");
    expect(blades?.basis).toBe("none");
    expect(a.health.nominal).toBe(2);
    expect(a.insight.lines.some((l) => l.kind === "unavailable")).toBe(true);
  });
});

describe("metric rules", () => {
  it("drops non-finite samples instead of imputing", () => {
    const s = rpmSeries([{ t: 1, v: 1 }, { t: 2, v: Number.NaN }, { t: 3, v: null }, { t: 4, v: Number.POSITIVE_INFINITY }, { t: 5, v: 2 }]);
    expect(validSamples(s).map((x) => x.v)).toEqual([1, 2]);
  });
  it("a series of only invalid samples is insufficient evidence", () => {
    const s = rpmSeries(ticks(WINDOW_START, AS_OF, () => null));
    expect(assessMetric(s, AS_OF, 5000).state).toBe("insufficient");
  });
  it("too little history is insufficient evidence", () => {
    const s = rpmSeries(ticks(AS_OF - 60_000, AS_OF, () => 1800));
    expect(assessMetric(s, AS_OF, 5000).state).toBe("insufficient");
  });
  it("a deviation shorter than the sustain window stays nominal", () => {
    const s = rpmSeries(ticks(WINDOW_START, AS_OF, (t) => (t > AS_OF - 60_000 ? 1400 : 1800)));
    const m = assessMetric(s, AS_OF, 5000);
    expect(m.state).toBe("nominal");
    expect(m.sustainedMs).toBeLessThan(SUSTAINED_MS);
  });
  it("a deviation exactly at the sustain window becomes attention", () => {
    const s = rpmSeries(ticks(WINDOW_START, AS_OF, (t) => (t >= AS_OF - SUSTAINED_MS ? 1400 : 1800)));
    expect(assessMetric(s, AS_OF, 5000).state).toBe("attention");
  });
  it("a deviation within 10% is not counted", () => {
    const s = rpmSeries(ticks(WINDOW_START, AS_OF, () => 1650));
    expect(assessMetric(s, AS_OF, 5000).state).toBe("nominal");
  });
  it("unavailable sensors never produce values", () => {
    const m = assessMetric(rpmSeries([], { sensorAvailable: false }), AS_OF, 5000);
    expect(m.state).toBe("unavailable");
    expect(m.latest).toBeNull();
  });
  it("out-of-order samples are sorted before use", () => {
    const s = rpmSeries([{ t: 20, v: 5 }, { t: 10, v: 1 }]);
    expect(validSamples(s).map((x) => x.t)).toEqual([10, 20]);
  });
  it("measures trailing runs as last minus first", () => {
    expect(trailingRunMs([{ t: 0, v: 1 }, { t: 5, v: 9 }, { t: 10, v: 9 }], (v) => v > 5)).toBe(5);
    expect(trailingRunMs([{ t: 0, v: 9 }, { t: 5, v: 1 }], (v) => v > 5)).toBe(0);
  });
});

describe("formatting", () => {
  it("formats numbers, percentages, durations and UTC clocks", () => {
    expect(formatNumber(1420, 0)).toBe("1420");
    expect(formatNumber(12345, 0)).toBe("12,345");
    expect(formatNumber(-0.001, 2)).toBe("0.00");
    expect(formatPercent(-0.2111)).toBe("−21%");
    expect(formatPercent(0.004)).toBe("0%");
    expect(formatDuration(210_000)).toBe("210 s");
    expect(formatDuration(1_800_000)).toBe("30 min");
    expect(formatClock(Date.UTC(2026, 9, 16, 10, 28, 12))).toBe("10:28:12");
  });
});

describe("report", () => {
  it("is deterministic and states it is a synthetic demonstration", () => {
    const a = assess(buildScenario("rpm-deviation"));
    const r = buildReport(a);
    expect(r).toBe(buildReport(assess(buildScenario("rpm-deviation"))));
    expect(r).toMatch(/no AI model was called/);
    expect(r).toMatch(/synthetic/);
    expect(r).toMatch(/not a diagnosis/);
    expect(r).toMatch(/4 of 5 nominal/);
    expect(r).toMatch(/5 of 7 checks evaluated/);
  });
});
