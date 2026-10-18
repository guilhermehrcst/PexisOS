import { FileText, MoreHorizontal, Sparkles } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import type { Insight, InsightKind } from "../domain/evidence";
import { useDismiss } from "../hooks/useDismiss";
import "./EvidenceInsight.css";

const KIND_LABEL: Readonly<Record<InsightKind, string>> = {
  measured: "Measured",
  inferred: "Inferred",
  insufficient: "Insufficient evidence",
  stale: "Stale data",
  unavailable: "Sensor unavailable",
};

interface Props {
  readonly insight: Insight;
  readonly onOpenReport: () => void;
}

export function insightText(i: Insight): string {
  return [
    `Observation: ${i.observation}`,
    `Status: ${i.status}`,
    `Evidence: ${i.evidence}`,
    `Confidence: ${i.confidence}.`,
    `Note: ${i.note}`,
  ].join("\n");
}

export function EvidenceInsight({ insight, onOpenReport }: Props) {
  const titleId = useId();
  const [menu, setMenu] = useState(false);
  const [copied, setCopied] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setMenu(false), []);
  useDismiss(menu, root, close);
  const kinds = Array.from(new Set(insight.lines.map((l) => l.kind)));

  const copy = async () => {
    setMenu(false);
    try {
      await navigator.clipboard.writeText(insightText(insight));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };

  return (
    <section className="insight card" aria-labelledby={titleId}>
      <div className="insight__head">
        <Sparkles size={22} strokeWidth={2.2} className="insight__spark" aria-hidden="true" />
        <h2 id={titleId} className="panel-title">AI Evidence Insight</h2>
        <span className="insight__chip">Read-only</span>
        <div className="insight__menu" ref={root}>
          <button type="button" className="insight__more" aria-label="Insight actions" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            <MoreHorizontal size={20} strokeWidth={2.4} />
          </button>
          {menu && (
            <div className="insight__popover" role="menu">
              <button type="button" role="menuitem" onClick={copy}>Copy insight text</button>
              <button type="button" role="menuitem" onClick={() => { setMenu(false); onOpenReport(); }}>Open full report</button>
            </div>
          )}
        </div>
      </div>
      <div className="insight__box">
        <FileText size={20} strokeWidth={2} className="insight__doc" aria-hidden="true" />
        <div className="insight__text">
          <p><b>Observation:</b> {insight.observation}</p>
          <p><b>Status:</b> {insight.status}</p>
          <p><b>Confidence:</b> {insight.confidence}.</p>
          <p><b>Note:</b> this is not a diagnosis.</p>
          <p className="insight__muted">Evidence: {insight.evidence}</p>
        </div>
      </div>
      <div className="insight__kinds" aria-label="Evidence types in this insight">
        {kinds.map((k) => <span key={k} className={`insight__kind insight__kind--${k}`}>{KIND_LABEL[k]}</span>)}
        <span className="insight__origin">Deterministic · synthetic data</span>
      </div>
      <span className="sr-only" role="status" aria-live="polite">{copied ? "Insight copied" : ""}</span>
    </section>
  );
}
