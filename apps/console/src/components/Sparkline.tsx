import { useId } from "react";
import { validSamples } from "../domain/evidence";
import type { MetricSeries } from "../domain/types";

interface Props {
  readonly series: MetricSeries;
  readonly width?: number;
  readonly height?: number;
  /** Down-sample to this many points so the line reads as a trend, not noise. */
  readonly points?: number;
}

/** Compact trend line with a soft area fill. Renders nothing meaningful for empty series. */
export function Sparkline({ series, width = 220, height = 40, points = 24 }: Props) {
  const gradientId = `spark-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const valid = validSamples(series);
  if (valid.length < 2) {
    return (
      <svg className="sparkline sparkline--empty" width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        <line x1="0" x2={width} y1={height / 2} y2={height / 2} stroke="#cbd5e1" strokeDasharray="3 4" />
      </svg>
    );
  }
  // Bucket means show the trend without inventing values; the last point stays the exact
  // latest sample so a recent change (such as the RPM drop) is never averaged away.
  const size = Math.max(1, Math.ceil(valid.length / points));
  const sampled: { t: number; v: number }[] = [];
  for (let i = 0; i < valid.length - 1; i += size) {
    const bucket = valid.slice(i, Math.min(i + size, valid.length - 1));
    sampled.push({
      t: bucket.reduce((a, b) => a + b.t, 0) / bucket.length,
      v: bucket.reduce((a, b) => a + (b.v as number), 0) / bucket.length,
    });
  }
  const lastSample = valid[valid.length - 1];
  if (lastSample) sampled.push({ t: lastSample.t, v: lastSample.v as number });
  const values = sampled.map((s) => s.v as number);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 1e-9) { min -= 1; max += 1; }
  const pad = (max - min) * 0.32;
  min -= pad; max += pad;
  const t0 = sampled[0]?.t ?? 0;
  const t1 = sampled[sampled.length - 1]?.t ?? 1;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * width;
  const y = (v: number) => height - ((v - min) / (max - min)) * height;
  const line = sampled.map((s, i) => `${i ? "L" : "M"}${x(s.t).toFixed(1)} ${y(s.v as number).toFixed(1)}`).join("");
  return (
    <svg className="sparkline" width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#1478ff" stopOpacity="0.16" />
          <stop offset="1" stopColor="#1478ff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line}L${width} ${height}L0 ${height}Z`} fill={`url(#${gradientId})`} />
      <path d={line} fill="none" stroke="#1f7bf2" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
