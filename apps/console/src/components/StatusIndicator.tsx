import { AlertTriangle, CheckCircle2, CircleSlash, Clock3, HelpCircle } from "lucide-react";
import type { EvidenceState } from "../domain/types";
import { STATE_LABEL } from "../domain/evidence";
import "./StatusIndicator.css";

const ICON = {
  nominal: CheckCircle2,
  attention: AlertTriangle,
  insufficient: HelpCircle,
  stale: Clock3,
  unavailable: CircleSlash,
} as const;

/** Small coloured dot for a state; text is supplied by the caller or defaults to the state label. */
export function StateDot({ state, label }: { state: EvidenceState; label?: string }) {
  return (
    <span className={`state-dot state-dot--${state}`} role="img" aria-label={label ?? STATE_LABEL[state]} />
  );
}

/** Filled icon badge used in status cards and lists. */
export function StateIcon({ state, size = 20 }: { state: EvidenceState; size?: number }) {
  const Icon = ICON[state];
  return (
    <span className={`state-icon state-icon--${state}`} aria-hidden="true">
      <Icon size={size} strokeWidth={2.4} />
    </span>
  );
}
