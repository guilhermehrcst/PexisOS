import { Info } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { DeviceVisualization } from "./components/DeviceVisualization";
import { DiagnosticHero } from "./components/DiagnosticHero";
import { EvidenceInsight } from "./components/EvidenceInsight";
import { MetricCard } from "./components/MetricCard";
import { ReportDialog } from "./components/ReportDialog";
import { Sidebar } from "./components/Sidebar";
import { SystemStatus } from "./components/SystemStatus";
import { TelemetryChart } from "./components/TelemetryChart";
import { Topbar } from "./components/Topbar";
import type { SearchTarget } from "./components/Topbar";
import { assess } from "./domain/evidence";
import { buildReport } from "./domain/report";
import { METRIC_ORDER, buildScenario, isScenarioId } from "./domain/scenario";
import type { MetricId, ScenarioId } from "./domain/types";
import type { HotspotId } from "./viz/fan3d";
import "./App.css";

const DEFAULT_SCENARIO: ScenarioId = "rpm-deviation";

/** Read the scenario from the URL; anything unknown falls back to the default (fail closed). */
export function scenarioFromSearch(search: string): ScenarioId {
  const value = new URLSearchParams(search).get("scenario");
  return isScenarioId(value) ? value : DEFAULT_SCENARIO;
}

export function App({ initialScenario }: { initialScenario?: ScenarioId }) {
  const [scenarioId, setScenarioId] = useState<ScenarioId>(
    () => initialScenario ?? scenarioFromSearch(typeof window === "undefined" ? "" : window.location.search),
  );
  const [selected, setSelected] = useState<HotspotId | null>(null);
  const [metric, setMetric] = useState<MetricId>("rpm");
  const [reportOpen, setReportOpen] = useState(false);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const vizRef = useRef<HTMLDivElement | null>(null);

  const assessment = useMemo(() => assess(buildScenario(scenarioId)), [scenarioId]);
  const report = useMemo(() => buildReport(assessment), [assessment]);

  const changeScenario = useCallback((id: ScenarioId) => {
    setScenarioId(id);
    const url = new URL(window.location.href);
    if (id === DEFAULT_SCENARIO) url.searchParams.delete("scenario");
    else url.searchParams.set("scenario", id);
    window.history.replaceState(null, "", url);
  }, []);

  const navigate = useCallback((t: SearchTarget) => {
    if (t.kind === "subsystem") {
      setSelected(t.id);
      vizRef.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    } else {
      setMetric(t.id);
      chartRef.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    }
  }, []);

  return (
    <div className="app">
      <Topbar assessment={assessment} onScenario={changeScenario} onNavigate={navigate} />
      <Sidebar />
      <main className="app__main" aria-label="Evidence diagnostic">
        <div className="app__hero">
          <DiagnosticHero assessment={assessment} onGenerateReport={() => setReportOpen(true)} />
          <div className="app__viz" ref={vizRef}>
            <DeviceVisualization subsystems={assessment.subsystems} selected={selected} onSelect={setSelected} />
          </div>
        </div>
      </main>
      <section className="app__metrics" aria-label="Telemetry readings">
        {METRIC_ORDER.map((id) => (
          <MetricCard key={id} series={assessment.scenario.series[id]} assessment={assessment.metrics[id]} />
        ))}
      </section>
      <section className="app__panels" aria-label="Analysis">
        <div ref={chartRef} className="app__panel-chart">
          <TelemetryChart scenario={assessment.scenario} metric={metric} onMetric={setMetric} />
        </div>
        <SystemStatus subsystems={assessment.subsystems} health={assessment.health} />
        <EvidenceInsight insight={assessment.insight} onOpenReport={() => setReportOpen(true)} />
      </section>
      <footer className="app__footer">
        <Info size={14} strokeWidth={2} aria-hidden="true" />
        <span>
          PexisOS analyzes operational data to provide evidence-based insights. This is not a diagnosis.
          <span className="app__footer-synthetic"> Synthetic demonstration data · no hardware connected · {assessment.scenario.label}</span>
        </span>
      </footer>
      {reportOpen && (
        <ReportDialog report={report} fileName={`pexisos-evidence-report-${assessment.scenario.id}.md`} onClose={() => setReportOpen(false)} />
      )}
    </div>
  );
}
