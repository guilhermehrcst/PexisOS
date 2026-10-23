import { useId } from "react";
import type { HealthSummary, SubsystemAssessment } from "../domain/evidence";
import { STATE_LABEL } from "../domain/evidence";
import type { EvidenceState } from "../domain/types";
import { StateDot } from "./StatusIndicator";
import "./SystemStatus.css";

const SEGMENT_COLOR: Readonly<Record<EvidenceState, string>> = {
  nominal: "var(--success-ring)",
  attention: "var(--warning)",
  insufficient: "#d5dde8",
  stale: "#c3ccd9",
  unavailable: "#e2e8f0",
};

interface Props {
  readonly subsystems: readonly SubsystemAssessment[];
  readonly health: HealthSummary;
}

/**
 * Subsystem status. The ring is a count of subsystems whose evidence is nominal, one
 * segment per subsystem. It is not a health score and implies no failure probability.
 */
export function SystemStatus({ subsystems, health }: Props) {
  const titleId = useId();
  const r = 50;
  const c = 2 * Math.PI * r;
  const gap = 5;
  const seg = c / subsystems.length;
  // Nominal segments first so the green arc reads continuously, like a gauge.
  const ordered = [...subsystems].sort((a, b) => Number(b.state === "nominal") - Number(a.state === "nominal"));
  return (
    <section className="health card" aria-labelledby={titleId}>
      <h2 id={titleId} className="panel-title">System Health</h2>
      <div className="health__body">
        <div className="health__ring" role="img" aria-label={`${health.nominal} of ${health.total} subsystems nominal`}>
          <svg viewBox="0 0 128 128" width="128" height="128" aria-hidden="true">
            <circle cx="64" cy="64" r={r} className="health__track" />
            {ordered.map((s, i) => (
              <circle
                key={s.id}
                cx="64"
                cy="64"
                r={r}
                fill="none"
                stroke={SEGMENT_COLOR[s.state]}
                strokeWidth="11"
                strokeLinecap="round"
                strokeDasharray={`${Math.max(0, seg - gap - 8)} ${c}`}
                strokeDashoffset={-(i * seg + gap / 2)}
                transform="rotate(-90 64 64)"
              />
            ))}
          </svg>
          <div className="health__center">
            <span className="health__count">{health.nominal}/{health.total}</span>
            <span className="health__caption">Nominal</span>
          </div>
        </div>
        <ul className="health__list">
          {subsystems.map((s) => (
            <li key={s.id}>
              <StateDot state={s.state} />
              <span className="health__name">{s.label}</span>
              <span className={`health__state health__state--${s.state}`}>{STATE_LABEL[s.state]}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="health__rule">Count of subsystems with nominal evidence, not a health score.</p>
    </section>
  );
}
