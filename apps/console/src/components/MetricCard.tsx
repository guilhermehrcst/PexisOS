import { Activity, Gauge, Thermometer, TrendingDown, TrendingUp, Zap } from "lucide-react";
import type { ReactNode } from "react";
import type { MetricAssessment } from "../domain/evidence";
import { STATE_LABEL, formatClock, formatDuration, formatNumber, formatPercent, isSignificantDeviation } from "../domain/evidence";
import type { MetricId, MetricSeries } from "../domain/types";
import { Sparkline } from "./Sparkline";
import { StateDot } from "./StatusIndicator";
import "./MetricCard.css";

function AmpereIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9.5" />
      <path d="M8.6 16.2 12 7.6l3.4 8.6M9.9 13h4.2" />
    </svg>
  );
}

const ICON: Readonly<Record<MetricId, ReactNode>> = {
  rpm: <Gauge size={24} strokeWidth={2.1} aria-hidden="true" />,
  temperature: <Thermometer size={24} strokeWidth={2.1} aria-hidden="true" />,
  voltage: <Zap size={24} strokeWidth={2.1} aria-hidden="true" />,
  current: <AmpereIcon />,
  vibration: <Activity size={24} strokeWidth={2.1} aria-hidden="true" />,
};

interface Props {
  readonly series: MetricSeries;
  readonly assessment: MetricAssessment;
}

function rangeText(series: MetricSeries): string {
  const { min, max } = series.normalRange;
  const d = series.decimals;
  if (series.id === "vibration") return `Expected ≤ ${formatNumber(max, d)} ${series.unit}`;
  return `Normal range ${formatNumber(min, d)} – ${formatNumber(max, d)} ${series.unit}`;
}

export function MetricCard({ series, assessment }: Props) {
  const latest = assessment.latest;
  const value = latest ? formatNumber(latest.value, series.decimals) : "—";
  const unitFirst = series.id === "vibration";
  const showValue = assessment.state !== "unavailable";
  return (
    <article className={`metric card metric--${assessment.state}`} aria-label={`${series.label}: ${showValue ? `${value} ${series.unit}` : "unavailable"}, ${STATE_LABEL[assessment.state]}`}>
      <div className="metric__head">
        <span className="metric__icon">{ICON[series.id]}</span>
        <div className="metric__heading">
          {unitFirst && <div className="metric__label metric__label--top">{series.shortLabel}</div>}
          <div className="metric__value">
            {showValue ? <>{value}<span className="metric__unit">{series.unit === "°C" ? " °C" : ` ${series.unit}`}</span></> : <span className="metric__na">No data</span>}
          </div>
          {!unitFirst && <div className="metric__label">{series.label}</div>}
          {assessment.state === "stale" && latest && <span className="metric__asof">Last value at {formatClock(latest.t)} UTC</span>}
        </div>
      </div>
      <div className="metric__spark"><Sparkline series={series} /></div>
      <div className="metric__foot">{footer(series, assessment)}</div>
    </article>
  );
}

function footer(series: MetricSeries, a: MetricAssessment): ReactNode {
  if (a.state === "stale") {
    return <><StateDot state="stale" /><span>Stale · last reading {formatDuration(a.latest?.ageMs ?? 0)} ago</span></>;
  }
  if (a.state === "unavailable" || a.state === "insufficient") {
    return <><StateDot state={a.state} /><span>{a.summary}</span></>;
  }
  if (series.baseline !== undefined) {
    const dev = a.deviation ?? 0;
    const significant = isSignificantDeviation(dev);
    const Trend = dev < 0 ? TrendingDown : TrendingUp;
    return (
      <>
        <span>Baseline {formatNumber(series.baseline, 0)} {series.unit}</span>
        <span className={`metric__delta${significant ? " is-significant" : ""}`}>
          <Trend size={14} strokeWidth={2.4} aria-hidden="true" />
          {formatPercent(dev)}
        </span>
      </>
    );
  }
  if (series.id === "vibration" && a.state === "nominal") {
    return <><StateDot state="nominal" /><span className="metric__ok">Stable</span><span className="metric__range">{rangeText(series)}</span></>;
  }
  return <><StateDot state={a.state} /><span>{rangeText(series)}</span></>;
}
