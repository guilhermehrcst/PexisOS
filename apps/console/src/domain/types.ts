/**
 * Domain types for the PexisOS Console demonstration.
 *
 * Every value shown by the console is derived from a `Scenario`. Scenarios are
 * synthetic: they are generated in code, never read from a device.
 */

export type MetricId = "rpm" | "temperature" | "voltage" | "current" | "vibration";

/** One sample. `v` is null when the sensor reported no value for that tick. */
export interface Sample {
  readonly t: number; // epoch milliseconds (UTC)
  readonly v: number | null;
}

export interface Range {
  readonly min: number;
  readonly max: number;
}

export interface MetricSeries {
  readonly id: MetricId;
  readonly label: string;
  readonly shortLabel: string;
  readonly unit: string;
  readonly decimals: number;
  /** False when the sensor is absent or not reporting at all. */
  readonly sensorAvailable: boolean;
  readonly normalRange: Range;
  /** Synthetic reference value, when the metric is compared to a baseline. */
  readonly baseline?: number;
  readonly samples: readonly Sample[];
}

export type ScenarioId = "rpm-deviation" | "nominal" | "stale-rpm" | "vibration-unavailable";

export interface Scenario {
  readonly id: ScenarioId;
  readonly label: string;
  readonly description: string;
  /** Always "synthetic": the console never presents hardware readings. */
  readonly provenance: "synthetic";
  readonly deviceId: string;
  readonly deviceName: string;
  /** The instant the console evaluates evidence at (end of the window). */
  readonly asOf: number;
  readonly windowStart: number;
  readonly sampleIntervalMs: number;
  readonly series: Readonly<Record<MetricId, MetricSeries>>;
}

/**
 * Evidence state of a reading or subsystem.
 * - nominal: evidence present, within expectation
 * - attention: evidence present, sustained deviation (a suspicion, never a diagnosis)
 * - insufficient: not enough evidence to say anything
 * - stale: the latest evidence is too old to describe the present
 * - unavailable: the sensor is not reporting
 */
export type EvidenceState = "nominal" | "attention" | "insufficient" | "stale" | "unavailable";

/** measured: read directly from a sensor. inferred: derived from other readings. */
export type EvidenceBasis = "measured" | "inferred" | "none";

export type Confidence = "high" | "medium" | "low" | "none";
