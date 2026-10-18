import { Bell, Eye, Fan, Search, UserRound } from "lucide-react";
import { useCallback, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { Assessment } from "../domain/evidence";
import { STATE_LABEL } from "../domain/evidence";
import { SCENARIO_IDS, scenarioLabel } from "../domain/scenario";
import type { MetricId, ScenarioId } from "../domain/types";
import { useDismiss } from "../hooks/useDismiss";
import type { HotspotId } from "../viz/fan3d";
import { Logo } from "./Logo";
import { Select } from "./Select";
import { StateDot } from "./StatusIndicator";
import "./Topbar.css";

export type SearchTarget = { kind: "subsystem"; id: HotspotId } | { kind: "metric"; id: MetricId };

interface Props {
  readonly assessment: Assessment;
  readonly onScenario: (id: ScenarioId) => void;
  readonly onNavigate: (target: SearchTarget) => void;
}

export function Topbar({ assessment, onScenario, onNavigate }: Props) {
  return (
    <header className="topbar">
      <div className="topbar__brand"><Logo /></div>
      <div className="topbar__device">
        <Select
          label="Device"
          value="lab-001"
          groups={[{ options: [{ value: "lab-001", label: "Lab 001 · Smart Fan", hint: "Synthetic device" }] }]}
          onChange={() => undefined}
          renderValue={(o) => (
            <>
              <span className="topbar__device-chip" aria-hidden="true"><Fan size={18} strokeWidth={2.4} /></span>
              <span className="topbar__device-name">{o?.label}</span>
            </>
          )}
          footer="Console V1 ships one synthetic device. No hardware is connected."
        />
      </div>
      <div className="topbar__mode">
        <Select<ScenarioId>
          label="Mode and scenario"
          value={assessment.scenario.id}
          groups={[
            { label: "Synthetic scenario", options: SCENARIO_IDS.map((id) => ({ value: id, label: scenarioLabel(id) })) },
          ]}
          onChange={onScenario}
          className="topbar__mode-select"
          renderValue={() => (
            <>
              <Eye size={18} strokeWidth={2.4} className="topbar__mode-icon" aria-hidden="true" />
              <span>Evidence Diagnostic</span>
            </>
          )}
          footer="Evidence Diagnostic is the only mode in V1. Scenarios replay synthetic data for demonstration."
        />
      </div>
      <SearchBox assessment={assessment} onNavigate={onNavigate} />
      <Notifications assessment={assessment} />
      <span className="topbar__avatar" role="img" aria-label="Demo operator (no account)" title="Demo operator: no account, no sign-in">
        <UserRound size={22} strokeWidth={2} />
      </span>
    </header>
  );
}

interface Result {
  readonly key: string;
  readonly label: string;
  readonly hint: string;
  readonly target: SearchTarget;
}

function SearchBox({ assessment, onNavigate }: { assessment: Assessment; onNavigate: (t: SearchTarget) => void }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement | null>(null);
  const listId = useId();
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);

  const index = useMemo<Result[]>(() => [
    ...assessment.subsystems.map((s) => ({ key: `s-${s.id}`, label: s.label, hint: `Component · ${STATE_LABEL[s.state]}`, target: { kind: "subsystem", id: s.id } as SearchTarget })),
    ...Object.values(assessment.scenario.series).map((m) => ({ key: `m-${m.id}`, label: m.label, hint: `Telemetry · ${m.unit}`, target: { kind: "metric", id: m.id } as SearchTarget })),
  ], [assessment]);
  const q = query.trim().toLowerCase();
  const results = q ? index.filter((r) => `${r.label} ${r.hint}`.toLowerCase().includes(q)).slice(0, 7) : [];

  const go = (r: Result) => {
    onNavigate(r.target);
    setQuery("");
    setOpen(false);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!results.length) return;
    // While the list is closed (for example after Escape), keys never act on hidden results;
    // the arrows reopen it, as in a standard combobox.
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setOpen(true); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (a + 1) % results.length); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (a - 1 + results.length) % results.length); }
    else if (e.key === "Enter") { e.preventDefault(); const r = results[active]; if (r) go(r); }
  };

  return (
    <div className="topbar__search" ref={root}>
      <Search size={20} strokeWidth={2.1} className="topbar__search-icon" aria-hidden="true" />
      <input
        type="search"
        role="combobox"
        aria-label="Search devices and evidence"
        aria-expanded={open && results.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && results[active] ? `${listId}-${active}` : undefined}
        placeholder="Search devices, evidence..."
        value={query}
        onChange={(e) => { setQuery(e.target.value); setActive(0); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
      />
      {open && q && (
        <ul className="topbar__results" id={listId} role="listbox" aria-label="Search results">
          {results.length === 0 && <li className="topbar__no-result" role="option" aria-selected={false} aria-disabled="true">No matching component or reading</li>}
          {results.map((r, i) => (
            <li
              key={r.key}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "is-active" : undefined}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => go(r)}
            >
              <span>{r.label}</span>
              <span className="topbar__result-hint">{r.hint}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Notifications({ assessment }: { assessment: Assessment }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, root, close);
  const items = assessment.subsystems.filter((s) => s.state !== "nominal");
  return (
    <div className="topbar__notify" ref={root}>
      <button
        type="button"
        className="topbar__icon-button"
        aria-label={`Notifications: ${items.length} item${items.length === 1 ? " needs" : "s need"} attention`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Bell size={21} strokeWidth={2.1} aria-hidden="true" />
        {items.length > 0 && <span className="topbar__badge" aria-hidden="true" />}
      </button>
      {open && (
        <div className="topbar__panel" role="region" aria-label="Notifications">
          <div className="topbar__panel-title">Evidence notifications</div>
          {items.length === 0 && <p className="topbar__panel-empty">No component needs attention.</p>}
          <ul>
            {items.map((s) => (
              <li key={s.id}>
                <StateDot state={s.state} />
                <span><strong>{s.label}</strong> · {STATE_LABEL[s.state]}<br /><span className="topbar__panel-detail">{s.detail}</span></span>
              </li>
            ))}
          </ul>
          <div className="topbar__panel-foot">Derived from synthetic data in this browser.</div>
        </div>
      )}
    </div>
  );
}
