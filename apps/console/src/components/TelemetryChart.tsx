import { useEffect, useId, useMemo, useState } from "react";
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { formatClock, formatNumber, validSamples } from "../domain/evidence";
import { METRIC_ORDER } from "../domain/scenario";
import type { MetricId, Scenario } from "../domain/types";
import { useElementSize } from "../hooks/useElementSize";
import { yScale } from "./chartScale";
import { Select } from "./Select";
import "./TelemetryChart.css";

interface Props {
  readonly scenario: Scenario;
  readonly metric: MetricId;
  readonly onMetric: (id: MetricId) => void;
}

const PAD = { top: 12, right: 18, bottom: 26, left: 44 };
const X_TICK_MS = 5 * 60 * 1000;

export function TelemetryChart({ scenario, metric, onMetric }: Props) {
  const series = scenario.series[metric];
  const [box, size] = useElementSize<HTMLDivElement>({ width: 520, height: 170 });
  const gradientId = `chart-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const titleId = useId();
  const samples = useMemo(() => validSamples(series), [series]);
  const [cursor, setCursor] = useState<number | null>(null);
  useEffect(() => setCursor(null), [metric, scenario.id]);

  const w = Math.max(160, size.width);
  const h = Math.max(110, size.height);
  const iw = w - PAD.left - PAD.right;
  const ih = h - PAD.top - PAD.bottom;
  const scale = yScale(samples.map((s) => s.v as number), series.baseline);
  const x = (t: number) => PAD.left + ((t - scenario.windowStart) / (scenario.asOf - scenario.windowStart)) * iw;
  const y = (v: number) => PAD.top + ih - ((v - scale.min) / (scale.max - scale.min)) * ih;

  // Break the line where samples are missing for more than two intervals (no interpolation).
  const segments: string[] = [];
  let current = "";
  let prevT: number | null = null;
  for (const s of samples) {
    const gap = prevT !== null && s.t - prevT > scenario.sampleIntervalMs * 2;
    if (gap && current) { segments.push(current); current = ""; }
    current += `${current ? "L" : "M"}${x(s.t).toFixed(1)} ${y(s.v as number).toFixed(1)}`;
    prevT = s.t;
  }
  if (current) segments.push(current);
  const line = segments.join("");
  const first = samples[0];
  const last = samples[samples.length - 1];
  const area = first && last && segments.length === 1
    ? `${line}L${x(last.t).toFixed(1)} ${PAD.top + ih}L${x(first.t).toFixed(1)} ${PAD.top + ih}Z`
    : "";

  const index = cursor ?? samples.length - 1;
  const point = samples[index];
  const xTicks: number[] = [];
  const tickMs = iw < 380 ? X_TICK_MS * 2 : X_TICK_MS; // keep labels from colliding on narrow screens
  for (let t = scenario.windowStart; t <= scenario.asOf; t += tickMs) xTicks.push(t);

  const nearest = (clientX: number, rect: DOMRect) => {
    const px = clientX - rect.left;
    const t = scenario.windowStart + ((px - PAD.left) / iw) * (scenario.asOf - scenario.windowStart);
    let best = 0;
    let bestD = Infinity;
    samples.forEach((s, i) => { const d = Math.abs(s.t - t); if (d < bestD) { bestD = d; best = i; } });
    return best;
  };
  const onMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (samples.length) setCursor(nearest(e.clientX, e.currentTarget.getBoundingClientRect()));
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!samples.length) return;
    const step = e.shiftKey ? 12 : 1;
    if (e.key === "ArrowLeft") { e.preventDefault(); setCursor(Math.max(0, index - step)); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setCursor(Math.min(samples.length - 1, index + step)); }
    else if (e.key === "Home") { e.preventDefault(); setCursor(0); }
    else if (e.key === "End") { e.preventDefault(); setCursor(samples.length - 1); }
  };

  const pointLabel = point ? `${formatNumber(point.v as number, series.decimals)} ${series.unit}` : "";
  const tooltipLeft = point ? Math.min(Math.max(x(point.t), PAD.left + 50), w - 52) : 0;
  const empty = !series.sensorAvailable || samples.length < 2;

  return (
    <section className="chart card" aria-labelledby={titleId}>
      <div className="chart__head">
        <h2 id={titleId} className="panel-title">Fan Telemetry <span className="panel-title__muted">(Last 30 minutes)</span></h2>
        <Select<MetricId>
          label="Telemetry metric"
          value={metric}
          align="end"
          className="chart__select"
          groups={[{ options: METRIC_ORDER.map((id) => ({ value: id, label: scenario.series[id].shortLabel })) }]}
          onChange={onMetric}
        />
      </div>
      <div className="chart__plot" ref={box}>
        {empty ? (
          <div className="chart__empty" role="status">
            {series.sensorAvailable ? "Not enough valid samples to draw this metric." : `${series.label} sensor unavailable: no data to plot.`}
          </div>
        ) : (
          <>
            <svg
              width={w}
              height={h}
              viewBox={`0 0 ${w} ${h}`}
              role="img"
              tabIndex={0}
              aria-label={`${series.label}, ${formatClock(scenario.windowStart, false)} to ${formatClock(scenario.asOf, false)} UTC. Selected point: ${pointLabel} at ${point ? formatClock(point.t) : ""}. Use left and right arrow keys to move.`}
              onPointerMove={onMove}
              onPointerLeave={() => setCursor(null)}
              onKeyDown={onKey}
              className="chart__svg"
            >
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor="#1478ff" stopOpacity="0.16" />
                  <stop offset="1" stopColor="#1478ff" stopOpacity="0.02" />
                </linearGradient>
              </defs>
              {scale.ticks.map((v) => (
                <g key={v}>
                  <line x1={PAD.left} x2={w - PAD.right} y1={y(v)} y2={y(v)} className="chart__grid" />
                  <text x={PAD.left - 10} y={y(v)} className="chart__tick" textAnchor="end" dominantBaseline="middle">{formatNumber(v, v % 1 ? 2 : 0)}</text>
                </g>
              ))}
              {xTicks.map((t) => (
                <g key={t}>
                  <line x1={x(t)} x2={x(t)} y1={PAD.top} y2={PAD.top + ih} className="chart__grid chart__grid--v" />
                  <text x={x(t)} y={h - 6} className="chart__tick" textAnchor="middle">{formatClock(t, false)}</text>
                </g>
              ))}
              {series.baseline !== undefined && (
                <g className="chart__baseline">
                  <line x1={PAD.left} x2={w - PAD.right} y1={y(series.baseline)} y2={y(series.baseline)} />
                  <text x={PAD.left + 6} y={y(series.baseline) - 6}>Synthetic baseline {formatNumber(series.baseline, 0)}</text>
                </g>
              )}
              {area && <path d={area} fill={`url(#${gradientId})`} />}
              <path d={line} className="chart__line" />
              {point && (
                <g>
                  <line x1={x(point.t)} x2={x(point.t)} y1={PAD.top} y2={PAD.top + ih} className="chart__cursor" />
                  <circle cx={x(point.t)} cy={y(point.v as number)} r="5" className={`chart__dot${cursor === null ? " is-latest" : ""}`} />
                </g>
              )}
            </svg>
            {point && (
              <div className="chart__tooltip" style={{ left: tooltipLeft, top: Math.max(0, y(point.v as number) - 62) }} aria-hidden="true">
                <strong>{pointLabel}</strong>
                <span>{formatClock(point.t)}</span>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
