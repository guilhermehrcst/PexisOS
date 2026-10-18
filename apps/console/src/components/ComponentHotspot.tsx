import { useId } from "react";
import type { LucideIcon } from "lucide-react";
import type { SubsystemAssessment } from "../domain/evidence";
import { STATE_LABEL } from "../domain/evidence";
import { StateDot } from "./StatusIndicator";
import "./ComponentHotspot.css";

export interface HotspotLayout {
  readonly x: number;
  readonly y: number;
  /** Which side of the fan the label sits on; the connector attaches to the inner edge. */
  readonly side: "left" | "right";
  readonly icon: LucideIcon;
}

const BASIS_TEXT = {
  measured: "Measured directly",
  inferred: "Inferred from other readings",
  none: "No usable evidence",
} as const;

interface Props {
  readonly subsystem: SubsystemAssessment;
  readonly layout: HotspotLayout;
  readonly selected: boolean;
  readonly onToggle: () => void;
}

export function ComponentHotspot({ subsystem, layout, selected, onToggle }: Props) {
  const detailId = useId();
  const Icon = layout.icon;
  return (
    <div
      className={`hotspot hotspot--${layout.side}${selected ? " is-selected" : ""} hotspot--${subsystem.state}`}
      style={{ left: `${layout.x * 100}%`, top: `${layout.y * 100}%` }}
    >
      <button
        type="button"
        className="hotspot__button"
        aria-expanded={selected}
        aria-controls={selected ? detailId : undefined}
        onClick={onToggle}
      >
        <span className="hotspot__icon" aria-hidden="true"><Icon size={17} strokeWidth={2.2} /></span>
        <span className="hotspot__text">
          <span className="hotspot__title">{subsystem.label}</span>
          <span className="hotspot__status">{subsystem.status}</span>
        </span>
        <span className="sr-only">, {STATE_LABEL[subsystem.state]}</span>
      </button>
      {selected && (
        <div className="hotspot__detail" id={detailId} role="region" aria-label={`${subsystem.label} evidence`}>
          <div className="hotspot__detail-row">
            <StateDot state={subsystem.state} />
            <strong>{STATE_LABEL[subsystem.state]}</strong>
            <span className="hotspot__chip">{BASIS_TEXT[subsystem.basis]}</span>
          </div>
          <p>{subsystem.detail}</p>
          <dl>
            <div><dt>Confidence</dt><dd>{subsystem.confidence}</dd></div>
            <div><dt>Sources</dt><dd>{subsystem.sources.join(", ")}</dd></div>
          </dl>
        </div>
      )}
    </div>
  );
}
