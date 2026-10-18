import { mulberry32, noise } from "./random";
import type { MetricId, MetricSeries, Range, Sample, Scenario, ScenarioId } from "./types";

/**
 * Synthetic scenarios for Lab 001 · Smart Fan.
 *
 * These numbers are written by this project to exercise the interface. They are
 * not measurements of any physical fan, and the console labels them as synthetic.
 */

export const WINDOW_START = Date.UTC(2026, 9, 16, 10, 0, 0); // 2026-10-16 10:00:00 UTC
export const WINDOW_MS = 30 * 60 * 1000;
export const SAMPLE_INTERVAL_MS = 5_000;
export const AS_OF = WINDOW_START + WINDOW_MS; // 10:30:00 UTC

export const RPM_BASELINE = 1800;
/** In the deviation scenarios the fan settles at this speed for the last 210 s. */
export const RPM_DEVIATED = 1420;
export const RPM_DEVIATION_START = AS_OF - 210_000; // 10:26:30 UTC
/** In the stale scenario the RPM feed stops reporting here (90 s before as-of). */
export const RPM_STALE_FROM = AS_OF - 90_000; // 10:28:30 UTC

interface MetricSpec {
  readonly id: MetricId;
  readonly label: string;
  readonly shortLabel: string;
  readonly unit: string;
  readonly decimals: number;
  readonly normalRange: Range;
  readonly baseline?: number;
  readonly centre: number;
  readonly jitter: number;
  readonly seed: number;
}

const SPECS: Readonly<Record<MetricId, MetricSpec>> = {
  rpm: {
    id: "rpm", label: "Rotational Speed", shortLabel: "RPM", unit: "RPM", decimals: 0,
    normalRange: { min: 1600, max: 2000 }, baseline: RPM_BASELINE,
    centre: RPM_BASELINE, jitter: 14, seed: 11,
  },
  temperature: {
    id: "temperature", label: "Temperature", shortLabel: "Temperature", unit: "°C", decimals: 0,
    normalRange: { min: 20, max: 60 }, centre: 34, jitter: 0.45, seed: 23,
  },
  voltage: {
    id: "voltage", label: "Input Voltage", shortLabel: "Voltage", unit: "V", decimals: 1,
    normalRange: { min: 11.5, max: 12.5 }, centre: 12.1, jitter: 0.025, seed: 37,
  },
  current: {
    id: "current", label: "Current Draw", shortLabel: "Current", unit: "A", decimals: 2,
    normalRange: { min: 0.1, max: 0.3 }, centre: 0.18, jitter: 0.004, seed: 41,
  },
  vibration: {
    id: "vibration", label: "Vibration", shortLabel: "Vibration", unit: "g", decimals: 2,
    normalRange: { min: 0, max: 0.3 }, centre: 0.12, jitter: 0.012, seed: 53,
  },
};

export const METRIC_ORDER: readonly MetricId[] = ["rpm", "temperature", "voltage", "current", "vibration"];

function round(value: number, decimals: number): number {
  const f = 10 ** (decimals + 1); // keep one extra digit so charts are not stepped
  return Math.round(value * f) / f;
}

function timeline(): number[] {
  const ticks: number[] = [];
  for (let t = WINDOW_START; t <= AS_OF; t += SAMPLE_INTERVAL_MS) ticks.push(t);
  return ticks;
}

function steady(spec: MetricSpec): Sample[] {
  const rand = mulberry32(spec.seed);
  const ticks = timeline();
  return ticks.map((t, i) => {
    // A slow drift plus noise keeps the line alive without looking random.
    const drift = Math.sin(i / 23) * spec.jitter * 0.6;
    const v = i === ticks.length - 1 ? spec.centre : spec.centre + drift + noise(rand) * spec.jitter;
    return { t, v: round(v, spec.decimals) };
  });
}

/**
 * RPM drops at RPM_DEVIATION_START and settles at the deviated speed over 10 s.
 * The onset sample is already more than 10% below the baseline, so the measured
 * sustained deviation is exactly AS_OF - RPM_DEVIATION_START (210 s).
 */
function deviatingRpm(spec: MetricSpec): Sample[] {
  const rand = mulberry32(spec.seed);
  const ticks = timeline();
  return ticks.map((t, i) => {
    const jitter = noise(rand) * spec.jitter;
    if (i === ticks.length - 1) return { t, v: RPM_DEVIATED };
    if (t < RPM_DEVIATION_START) {
      return { t, v: round(RPM_BASELINE + Math.sin(i / 23) * 12 + jitter, 0) };
    }
    const settle = Math.min(1, (t - RPM_DEVIATION_START + SAMPLE_INTERVAL_MS) / 10_000);
    const target = RPM_BASELINE + (RPM_DEVIATED - RPM_BASELINE) * settle;
    return { t, v: round(target + jitter * 0.8, 0) };
  });
}

function series(spec: MetricSpec, samples: Sample[], sensorAvailable = true): MetricSeries {
  const base = {
    id: spec.id, label: spec.label, shortLabel: spec.shortLabel, unit: spec.unit,
    decimals: spec.decimals, normalRange: spec.normalRange, sensorAvailable, samples,
  };
  return spec.baseline === undefined ? base : { ...base, baseline: spec.baseline };
}

const SCENARIO_TEXT: Readonly<Record<ScenarioId, { label: string; description: string }>> = {
  "rpm-deviation": {
    label: "RPM below baseline",
    description: "Fan speed settles 21% below its synthetic baseline for the last 210 s; other readings stay in range.",
  },
  nominal: {
    label: "All readings nominal",
    description: "Every synthetic reading stays within its expected range for the whole window.",
  },
  "stale-rpm": {
    label: "Stale RPM feed",
    description: "The RPM deviation starts, then the RPM feed stops reporting 90 s before the evaluation time.",
  },
  "vibration-unavailable": {
    label: "Vibration sensor unavailable",
    description: "The vibration sensor never reports, so blade and vibration evidence is insufficient.",
  },
};

export const SCENARIO_IDS: readonly ScenarioId[] = ["rpm-deviation", "nominal", "stale-rpm", "vibration-unavailable"];

export function isScenarioId(value: unknown): value is ScenarioId {
  return typeof value === "string" && (SCENARIO_IDS as readonly string[]).includes(value);
}

export function scenarioLabel(id: ScenarioId): string {
  return SCENARIO_TEXT[id].label;
}

/** Build a scenario. Pure and deterministic: equal ids give deep-equal scenarios. */
export function buildScenario(id: ScenarioId): Scenario {
  const steadyOf = (metric: MetricId) => series(SPECS[metric], steady(SPECS[metric]));
  let rpm: MetricSeries;
  let vibration = steadyOf("vibration");

  switch (id) {
    case "nominal":
      rpm = steadyOf("rpm");
      break;
    case "rpm-deviation":
      rpm = series(SPECS.rpm, deviatingRpm(SPECS.rpm));
      break;
    case "stale-rpm":
      rpm = series(SPECS.rpm, deviatingRpm(SPECS.rpm).filter((s) => s.t < RPM_STALE_FROM));
      break;
    case "vibration-unavailable":
      rpm = series(SPECS.rpm, deviatingRpm(SPECS.rpm));
      vibration = series(SPECS.vibration, [], false);
      break;
  }

  return {
    id,
    ...SCENARIO_TEXT[id],
    provenance: "synthetic",
    deviceId: "lab-001",
    deviceName: "Lab 001 · Smart Fan",
    asOf: AS_OF,
    windowStart: WINDOW_START,
    sampleIntervalMs: SAMPLE_INTERVAL_MS,
    series: {
      rpm,
      temperature: steadyOf("temperature"),
      voltage: steadyOf("voltage"),
      current: steadyOf("current"),
      vibration,
    },
  };
}
