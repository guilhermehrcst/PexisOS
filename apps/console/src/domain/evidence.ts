import type {
  Confidence, EvidenceBasis, EvidenceState, MetricId, MetricSeries, Sample, Scenario,
} from "./types";

/**
 * Deterministic evidence rules. No model, no randomness, no clock: the only input
 * is a Scenario, so the same scenario always yields the same assessment.
 *
 * The rules describe observations. They never name a cause and never diagnose.
 */

/** A reading older than this (relative to scenario.asOf) is stale. */
export const STALE_AFTER_MS = 30_000;
/** A deviation from the baseline must exceed this fraction to count. */
export const DEVIATION_THRESHOLD = 0.1;
/** A deviation must persist at least this long before it becomes a suspicion. */
export const SUSTAINED_MS = 120_000;
/** Minimum span of valid data needed before any metric can be assessed. */
export const MIN_EVIDENCE_SPAN_MS = SUSTAINED_MS;

export interface Latest {
  readonly value: number;
  readonly t: number;
  readonly ageMs: number;
}

export interface MetricAssessment {
  readonly id: MetricId;
  readonly state: EvidenceState;
  readonly basis: EvidenceBasis;
  readonly latest: Latest | null;
  /** Signed fraction vs baseline (e.g. -0.211), when the metric has a baseline. */
  readonly deviation: number | null;
  /** Length of the trailing run beyond the threshold or outside the range, in ms. */
  readonly sustainedMs: number;
  readonly inRange: boolean | null;
  readonly summary: string;
}

/** Samples with a finite value, in time order. Invalid ticks are dropped, never imputed. */
export function validSamples(series: MetricSeries): Sample[] {
  return series.samples
    .filter((s): s is { t: number; v: number } => typeof s.v === "number" && Number.isFinite(s.v) && Number.isFinite(s.t))
    .slice()
    .sort((a, b) => a.t - b.t);
}

export function latestOf(series: MetricSeries, asOf: number): Latest | null {
  const valid = validSamples(series);
  const last = valid[valid.length - 1];
  if (!last || last.v === null) return null;
  return { value: last.v, t: last.t, ageMs: Math.max(0, asOf - last.t) };
}

/** Duration of the trailing run of samples for which `outside` holds (last - first, ms). */
export function trailingRunMs(samples: readonly Sample[], outside: (v: number) => boolean): number {
  let first: number | null = null;
  let last: number | null = null;
  for (let i = samples.length - 1; i >= 0; i--) {
    const s = samples[i];
    if (!s || s.v === null || !outside(s.v)) break;
    first = s.t;
    if (last === null) last = s.t;
  }
  return first === null || last === null ? 0 : last - first;
}

function inRange(series: MetricSeries, v: number): boolean {
  return v >= series.normalRange.min && v <= series.normalRange.max;
}

export function assessMetric(series: MetricSeries, asOf: number): MetricAssessment {
  const base = { id: series.id, deviation: null, sustainedMs: 0, inRange: null } as const;
  if (!series.sensorAvailable) {
    return { ...base, state: "unavailable", basis: "none", latest: null, summary: "Sensor unavailable: no readings reported" };
  }
  const valid = validSamples(series);
  const latest = latestOf(series, asOf);
  const first = valid[0];
  if (!latest || !first) {
    return { ...base, state: "insufficient", basis: "none", latest: null, summary: "No valid readings in the window" };
  }
  const range = inRange(series, latest.value);
  const deviation = series.baseline === undefined ? null : (latest.value - series.baseline) / series.baseline;
  if (latest.ageMs > STALE_AFTER_MS) {
    return {
      ...base, state: "stale", basis: "measured", latest, deviation, inRange: range,
      summary: `Last reading ${formatDuration(latest.ageMs)} old: current state unknown`,
    };
  }
  if (latest.t - first.t < MIN_EVIDENCE_SPAN_MS) {
    return { ...base, state: "insufficient", basis: "measured", latest, deviation, inRange: range, summary: "Too little history to assess" };
  }

  let sustainedMs: number;
  if (series.baseline !== undefined) {
    const baseline = series.baseline;
    sustainedMs = trailingRunMs(valid, (v) => Math.abs(v - baseline) / baseline > DEVIATION_THRESHOLD);
  } else {
    sustainedMs = trailingRunMs(valid, (v) => !inRange(series, v));
  }
  const sustained = sustainedMs >= SUSTAINED_MS;
  if (sustained) {
    const what = deviation === null ? "outside its expected range" : `${formatPercent(deviation)} vs baseline`;
    return {
      ...base, state: "attention", basis: "measured", latest, deviation, sustainedMs, inRange: range,
      summary: `${what} for ${formatDuration(sustainedMs)}`,
    };
  }
  return {
    ...base, state: "nominal", basis: "measured", latest, deviation, sustainedMs, inRange: range,
    summary: sustainedMs > 0 ? "Brief deviation, not sustained" : "Within expected range",
  };
}

/* ------------------------------------------------------------------ subsystems */

export type SubsystemId = "motor" | "blades" | "powerInput" | "rpmSensor" | "vibration";

export interface SubsystemAssessment {
  readonly id: SubsystemId;
  readonly label: string;
  readonly state: EvidenceState;
  readonly basis: EvidenceBasis;
  readonly confidence: Confidence;
  /** Short status for hotspots and lists. */
  readonly status: string;
  readonly detail: string;
  readonly sources: readonly MetricId[];
}

const SEVERITY: Readonly<Record<EvidenceState, number>> = {
  nominal: 0, insufficient: 1, unavailable: 2, stale: 3, attention: 4,
};

function worst(...states: EvidenceState[]): EvidenceState {
  return states.reduce((a, b) => (SEVERITY[b] > SEVERITY[a] ? b : a), "nominal");
}

function blocked(state: EvidenceState): boolean {
  return state === "stale" || state === "unavailable" || state === "insufficient";
}

export const STATE_LABEL: Readonly<Record<EvidenceState, string>> = {
  nominal: "Good",
  attention: "Attention",
  insufficient: "Insufficient",
  stale: "Stale",
  unavailable: "Unavailable",
};

export function assessSubsystems(m: Readonly<Record<MetricId, MetricAssessment>>): SubsystemAssessment[] {
  const { rpm, voltage, current, vibration } = m;

  const powerState = worst(voltage.state, current.state);
  const power: SubsystemAssessment = {
    id: "powerInput", label: "Power Input", state: powerState, basis: "measured",
    confidence: powerState === "nominal" ? "high" : blocked(powerState) ? "none" : "medium",
    status: voltage.latest && voltage.state !== "unavailable" ? `${formatNumber(voltage.latest.value, 1)} V` : STATE_LABEL[powerState],
    detail: powerState === "nominal" ? "Voltage and current within expected range" : `Voltage: ${voltage.summary}. Current: ${current.summary}.`,
    sources: ["voltage", "current"],
  };

  const rpmSensor: SubsystemAssessment = {
    id: "rpmSensor", label: "RPM Sensor", state: rpm.state, basis: rpm.basis,
    confidence: rpm.state === "attention" ? "medium" : rpm.state === "nominal" ? "high" : "none",
    status: rpm.state === "attention" ? "Below baseline" : rpm.state === "nominal" ? "Monitoring" : STATE_LABEL[rpm.state],
    detail: rpm.state === "attention" ? `Reading ${rpm.summary}. Cause not determined.` : rpm.summary,
    sources: ["rpm"],
  };

  const electrical = worst(voltage.state, current.state);
  const motorState: EvidenceState = blocked(electrical) ? "insufficient" : electrical;
  const motor: SubsystemAssessment = {
    id: "motor", label: "Motor", state: motorState, basis: motorState === "insufficient" ? "none" : "inferred",
    confidence: motorState === "nominal" ? "medium" : "none",
    status: motorState === "nominal" ? "OK" : STATE_LABEL[motorState],
    detail: motorState === "nominal"
      ? "Inferred: electrical draw within range. No motor sensor; this does not explain the speed reading."
      : "No direct motor sensor and electrical evidence is incomplete.",
    sources: ["voltage", "current"],
  };

  const bladesState: EvidenceState = blocked(vibration.state) ? "insufficient" : vibration.state;
  const blades: SubsystemAssessment = {
    id: "blades", label: "Blades", state: bladesState, basis: bladesState === "insufficient" ? "none" : "inferred",
    confidence: bladesState === "nominal" ? "low" : bladesState === "attention" ? "low" : "none",
    status: bladesState === "nominal" ? "Normal" : STATE_LABEL[bladesState],
    detail: bladesState === "insufficient"
      ? "Needs vibration evidence, which is not available."
      : "Inferred from overall vibration only; no spectral or visual evidence.",
    sources: ["vibration"],
  };

  const vib: SubsystemAssessment = {
    id: "vibration", label: "Vibration", state: vibration.state, basis: vibration.basis,
    confidence: vibration.state === "nominal" ? "high" : vibration.state === "attention" ? "medium" : "none",
    status: vibration.state === "nominal" ? "Stable" : STATE_LABEL[vibration.state],
    detail: vibration.summary,
    sources: ["vibration"],
  };

  return [motor, blades, power, rpmSensor, vib];
}

/* ------------------------------------------------------------- evidence checks */

export interface EvidenceCheck {
  readonly id: string;
  readonly label: string;
  /** evaluated: inputs present and fresh, so the check produced a result. */
  readonly evaluated: boolean;
  readonly reason: string;
}

export function evidenceChecks(m: Readonly<Record<MetricId, MetricAssessment>>): EvidenceCheck[] {
  const fromMetric = (id: string, label: string, a: MetricAssessment): EvidenceCheck => ({
    id, label, evaluated: !blocked(a.state), reason: blocked(a.state) ? a.summary : "Evaluated",
  });
  return [
    fromMetric("rpm-baseline", "RPM vs synthetic baseline", m.rpm),
    fromMetric("voltage-range", "Input voltage range", m.voltage),
    fromMetric("current-range", "Current draw range", m.current),
    fromMetric("temperature-range", "Temperature range", m.temperature),
    fromMetric("vibration-level", "Overall vibration level", m.vibration),
    {
      id: "blade-spectrum", label: "Blade imbalance (vibration spectrum)", evaluated: false,
      reason: "No spectral vibration data in this scenario",
    },
    {
      id: "bearing-trend", label: "Bearing wear trend", evaluated: false,
      reason: "Needs at least 7 days of history; the window is 30 min",
    },
  ];
}

/* --------------------------------------------------------------------- health */

export interface HealthSummary {
  readonly nominal: number;
  readonly total: number;
  /** nominal / total; a count of subsystems, not a health score. */
  readonly fraction: number;
}

export function healthSummary(subsystems: readonly SubsystemAssessment[]): HealthSummary {
  const nominal = subsystems.filter((s) => s.state === "nominal").length;
  return { nominal, total: subsystems.length, fraction: subsystems.length ? nominal / subsystems.length : 0 };
}

/* -------------------------------------------------------------------- insight */

export type InsightKind = "measured" | "inferred" | "insufficient" | "stale" | "unavailable";

export interface InsightLine {
  readonly kind: InsightKind;
  readonly text: string;
}

export interface Insight {
  readonly state: EvidenceState;
  readonly observation: string;
  readonly status: string;
  readonly evidence: string;
  readonly confidence: Confidence;
  readonly note: string;
  readonly lines: readonly InsightLine[];
}

export function buildInsight(
  m: Readonly<Record<MetricId, MetricAssessment>>,
  subsystems: readonly SubsystemAssessment[],
): Insight {
  const rpm = m.rpm;
  const note = "This is not a diagnosis. It is an evidence-based observation derived from synthetic sensor data.";
  const lines: InsightLine[] = [];

  for (const s of subsystems) {
    if (s.state === "stale" || s.state === "unavailable" || s.state === "insufficient") {
      lines.push({ kind: s.state, text: `${s.label}: ${s.detail}` });
    } else if (s.basis === "inferred") {
      lines.push({ kind: "inferred", text: `${s.label}: ${s.detail}` });
    }
  }

  if (rpm.state === "attention" && rpm.deviation !== null) {
    lines.unshift({ kind: "measured", text: `fan_rpm ${formatNumber(rpm.latest?.value ?? 0, 0)} RPM, ${formatPercent(rpm.deviation)} vs 1800 RPM synthetic baseline` });
    return {
      state: "attention",
      observation: `fan_rpm remains ${rpm.deviation < 0 ? "below" : "above"} the baseline for ${formatDuration(rpm.sustainedMs)}.`,
      status: "suspected anomaly.",
      evidence: `sustained ${formatPercent(rpm.deviation)} deviation from the synthetic reference; voltage and current in range.`,
      confidence: "medium",
      note, lines,
    };
  }
  if (rpm.state === "stale") {
    lines.unshift({ kind: "stale", text: `fan_rpm: ${rpm.summary}` });
    return {
      state: "stale",
      observation: `the RPM feed stopped reporting ${formatDuration(rpm.latest?.ageMs ?? 0)} ago.`,
      status: "cannot assess the current speed.",
      evidence: "the last fresh RPM readings were below the baseline, but they no longer describe the present.",
      confidence: "none",
      note, lines,
    };
  }
  if (rpm.state === "unavailable" || rpm.state === "insufficient") {
    return {
      state: rpm.state,
      observation: "no usable RPM evidence in the window.",
      status: "insufficient evidence.",
      evidence: rpm.summary,
      confidence: "none",
      note, lines,
    };
  }
  lines.unshift({ kind: "measured", text: `fan_rpm ${formatNumber(rpm.latest?.value ?? 0, 0)} RPM, within the expected range` });
  return {
    state: "nominal",
    observation: "all fresh readings stay within their expected ranges.",
    status: "no deviation detected.",
    evidence: "no sustained deviation from the synthetic references.",
    confidence: lines.some((l) => l.kind !== "measured" && l.kind !== "inferred") ? "low" : "medium",
    note, lines,
  };
}

/* ------------------------------------------------------------------- top level */

export interface Assessment {
  readonly scenario: Scenario;
  readonly metrics: Readonly<Record<MetricId, MetricAssessment>>;
  readonly subsystems: readonly SubsystemAssessment[];
  readonly checks: readonly EvidenceCheck[];
  readonly coverage: { readonly evaluated: number; readonly total: number; readonly fraction: number };
  readonly health: HealthSummary;
  readonly insight: Insight;
}

export function assess(scenario: Scenario): Assessment {
  const s = scenario.series;
  const metrics = {
    rpm: assessMetric(s.rpm, scenario.asOf),
    temperature: assessMetric(s.temperature, scenario.asOf),
    voltage: assessMetric(s.voltage, scenario.asOf),
    current: assessMetric(s.current, scenario.asOf),
    vibration: assessMetric(s.vibration, scenario.asOf),
  };
  const subsystems = assessSubsystems(metrics);
  const checks = evidenceChecks(metrics);
  const evaluated = checks.filter((c) => c.evaluated).length;
  return {
    scenario, metrics, subsystems, checks,
    coverage: { evaluated, total: checks.length, fraction: evaluated / checks.length },
    health: healthSummary(subsystems),
    insight: buildInsight(metrics, subsystems),
  };
}

/* ------------------------------------------------------------------ formatting */

const MINUS = "−";

export function formatNumber(value: number, decimals: number): string {
  const fixed = Math.abs(value).toFixed(decimals);
  const [int = "0", frac] = fixed.split(".");
  const grouped = int.length > 4 ? int.replace(/\B(?=(\d{3})+(?!\d))/g, ",") : int;
  const body = frac === undefined ? grouped : `${grouped}.${frac}`;
  return value < 0 && Number(fixed) !== 0 ? `${MINUS}${body}` : body;
}

export function formatPercent(fraction: number): string {
  const pct = Math.round(fraction * 100);
  if (pct === 0) return "0%";
  return pct < 0 ? `${MINUS}${Math.abs(pct)}%` : `+${pct}%`;
}

export function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 600) return `${s} s`;
  const min = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${min} min ${rest} s` : `${min} min`;
}

/** HH:MM:SS in UTC, so output does not depend on the viewer's time zone. */
export function formatClock(t: number, withSeconds = true): string {
  const d = new Date(t);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return withSeconds ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`;
}
