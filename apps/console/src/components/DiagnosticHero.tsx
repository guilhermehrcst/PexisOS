import { ChevronRight, Sparkles } from "lucide-react";
import type { Assessment } from "../domain/evidence";
import { formatDuration, formatPercent } from "../domain/evidence";
import type { EvidenceState } from "../domain/types";
import { StateIcon } from "./StatusIndicator";
import "./DiagnosticHero.css";

interface Props {
  readonly assessment: Assessment;
  readonly onGenerateReport: () => void;
}

interface StatusCard {
  readonly key: string;
  readonly state: EvidenceState;
  readonly title: string;
  readonly detail: string;
  /** Extra evidence shown as a tooltip and to assistive technology. */
  readonly hint?: string;
}

/** The two headline status cards: RPM evidence and power evidence. */
export function statusCards(a: Assessment): StatusCard[] {
  const rpm = a.metrics.rpm;
  const power = a.subsystems.find((s) => s.id === "powerInput");
  const rpmCard: StatusCard =
    rpm.state === "attention" && rpm.deviation !== null
      ? { key: "rpm", state: "attention", title: "RPM anomaly", detail: `${rpm.deviation < 0 ? "Below" : "Above"} baseline`, hint: `${formatPercent(rpm.deviation)} for ${formatDuration(rpm.sustainedMs)}` }
      : rpm.state === "nominal"
        ? { key: "rpm", state: "nominal", title: "RPM steady", detail: "Near synthetic baseline" }
        : rpm.state === "stale"
          ? { key: "rpm", state: "stale", title: "RPM data stale", detail: `Last reading ${formatDuration(rpm.latest?.ageMs ?? 0)} ago`, hint: rpm.summary }
          : { key: "rpm", state: rpm.state, title: "RPM evidence missing", detail: "No usable readings", hint: rpm.summary };
  const powerCard: StatusCard =
    power && power.state === "nominal"
      ? { key: "power", state: "nominal", title: "Power stable", detail: "Within expected range" }
      : { key: "power", state: power?.state ?? "insufficient", title: "Power needs review", detail: "See Power Input", hint: power?.detail ?? "No evidence" };
  return [rpmCard, powerCard];
}

export function DiagnosticHero({ assessment, onGenerateReport }: Props) {
  const { coverage } = assessment;
  const pct = Math.round(coverage.fraction * 100);
  return (
    <div className="hero" id="overview">
      <p className="hero__eyebrow">Lab 001 <span aria-hidden="true">·</span> Smart Fan</p>
      <h1 className="hero__title">Evidence<br />Diagnostic</h1>

      <div className="hero__coverage">
        <div className="hero__coverage-head">
          <span className="hero__coverage-value">{pct}%</span>
          <span className="hero__coverage-label">Evidence coverage</span>
          <span className="hero__coverage-count">{coverage.evaluated} of {coverage.total} checks</span>
        </div>
        <div
          className="hero__bar"
          role="progressbar"
          aria-label="Evidence coverage: checks with enough fresh evidence to evaluate"
          aria-valuemin={0}
          aria-valuemax={coverage.total}
          aria-valuenow={coverage.evaluated}
          aria-valuetext={`${coverage.evaluated} of ${coverage.total} checks evaluated`}
        >
          <span style={{ width: `${pct}%` }} />
        </div>
      </div>

      <div className="hero__cards">
        {statusCards(assessment).map((c) => (
          <div className={`hero__status hero__status--${c.state}`} key={c.key} title={c.hint}>
            <StateIcon state={c.state} size={22} />
            <div>
              <div className="hero__status-title">{c.title}</div>
              <div className="hero__status-detail">
                {c.detail}
                {c.hint && <span className="sr-only">, {c.hint}</span>}
              </div>
            </div>
          </div>
        ))}
      </div>

      <button type="button" className="hero__cta" onClick={onGenerateReport} aria-describedby="cta-note">
        <Sparkles size={20} strokeWidth={2.2} aria-hidden="true" />
        <span>Generate AI Report</span>
        <ChevronRight size={18} strokeWidth={2.4} aria-hidden="true" className="hero__cta-chevron" />
      </button>
      <p id="cta-note" className="hero__cta-note">Demo · deterministic report from synthetic data. No AI model is called.</p>
    </div>
  );
}
