"""Reproducible offline benchmark: identical synthetic splits for every detector.

Seeds are split once and never mixed: DEV_SEEDS were used while developing the
generator and detector; TEST_SEEDS are reported. Detectors receive telemetry
rows only (never the ``Dataset``); ground truth goes only to ``evaluate.score``.
Results are deterministic (``results.json``); timings and environment go to
``provenance.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .baselines import Verdict, fixed_limit, legacy_v01
from .config import DetectorConfig
from .detect import assess_records
from .evaluate import Score, metrics, score
from .ingest import Record, parse_rows
from .reference import build_reference
from .synth import SCENARIOS, Row, generate

DEV_SEEDS = (101, 102, 103, 104, 105)
TEST_SEEDS = tuple(range(201, 211))

# Declared criteria for the mechanical hypothesis verdicts (ADR 0001 §7).
COVERAGE_FLOOR = 0.2          # a mostly silent detector cannot support a "fewer errors" claim
NON_INFERIORITY_MARGIN = 0.1  # excess recall may be at most this much below the comparator


@dataclass(frozen=True)
class Profile:
    name: str
    split: str
    seeds: tuple[int, ...]
    reference_days: int
    eval_days: int


PROFILES = {
    "quick": Profile("quick", "dev", DEV_SEEDS[:2], 6, 14),
    "dev": Profile("dev", "dev", DEV_SEEDS, 10, 20),
    "full": Profile("full", "test", TEST_SEEDS, 10, 20),
}

BASE = DetectorConfig()
V02_VARIANTS: dict[str, tuple[DetectorConfig, str]] = {
    "v0.2": (BASE, "defaults (ADR 0001 §4, corrected after review in §7)"),
    "v0.2-no-warmup": (replace(BASE, warmup_s=0.0), "ablation: warm-up suppression off"),
    "v0.2-fixed-8C": (replace(BASE, fixed_threshold_c=8.0), "ablation: v0.1's fixed 8 C threshold"),
    "v0.2-fixed-4C": (replace(BASE, fixed_threshold_c=4.0),
                      "ablation: plain fixed 4 C threshold (what does the robust scale add?)"),
    "v0.2-no-load": (replace(BASE, use_load=False), "ablation: engine-load context ignored"),
    "v0.2-no-absolute": (replace(BASE, absolute_high_c=None), "ablation: absolute 115 C barrier off"),
    "v0.2-no-stuck": (replace(BASE, stuck_gap_s=1e9), "ablation: stuck-sensor rule off"),
}
DETECTOR_NOTES = {
    "fixed-limit-115C": "naive warning light: >= 115 C for 2 consecutive samples; no reference, time or quality logic",
    "v0.1-as-shipped": "frozen v0.1 rule on raw rows; the run fails if v0.1 rejects any row",
    "v0.1-cleaned": "frozen v0.1 rule after oracle-free cleaning (drop unparseable rows, sort, de-duplicate)",
    **{name: note for name, (_, note) in V02_VARIANTS.items()},
}
DETECTORS = tuple(DETECTOR_NOTES)

# A runner sees telemetry only: raw rows and the same rows parsed by v0.2 ingest.
Runner = Callable[[Sequence[Row], Sequence[Row], list[Record], list[Record]], "list[Verdict] | None"]


def _v02(config: DetectorConfig) -> Runner:
    def run(reference_rows: Sequence[Row], observation_rows: Sequence[Row],
            reference: list[Record], observed: list[Record]) -> list[Verdict]:
        model = build_reference(reference, config, use_load=config.use_load)
        return [(a.status, a.direction) for a in assess_records(model, observed)]
    return run


RUNNERS: dict[str, Runner] = {
    "fixed-limit-115C": lambda ref_rows, obs_rows, ref, obs: fixed_limit(obs_rows),
    "v0.1-as-shipped": lambda ref_rows, obs_rows, ref, obs: legacy_v01(ref_rows, obs_rows, cleaned=False),
    "v0.1-cleaned": lambda ref_rows, obs_rows, ref, obs: legacy_v01(ref_rows, obs_rows, cleaned=True),
    **{name: _v02(config) for name, (config, _) in V02_VARIANTS.items()},
}


def run_benchmark(profile: Profile, scenarios: Sequence[str] | None = None,
                  progress: Callable[[str], None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    names = list(scenarios or SCENARIOS)
    totals: dict[tuple[str, str], Score] = defaultdict(Score)
    seconds: dict[str, float] = defaultdict(float)
    rows_processed: dict[str, int] = defaultdict(int)
    started = time.perf_counter()
    for scenario in names:
        for seed in profile.seeds:
            t0 = time.perf_counter()
            data = generate(scenario, seed, reference_days=profile.reference_days, eval_days=profile.eval_days)
            seconds["generate"] += time.perf_counter() - t0
            t0 = time.perf_counter()
            reference, observed = list(parse_rows(data.reference_rows)), list(parse_rows(data.observation_rows))
            seconds["parse (v0.2 ingest)"] += time.perf_counter() - t0
            for detector in DETECTORS:
                t0 = time.perf_counter()
                verdicts = RUNNERS[detector](data.reference_rows, data.observation_rows, reference, observed)
                seconds[detector] += time.perf_counter() - t0
                rows_processed[detector] += len(data.observation_rows) + len(data.reference_rows)
                totals[(scenario, detector)].add(score(verdicts, data.truth))
            if progress:
                progress(f"{scenario} seed {seed}: {len(data.observation_rows)} observation rows")

    t0 = time.perf_counter()
    results: dict[str, Any] = {
        "schema": "pexislink.benchmark.v2",
        "notice": ("Synthetic data generated by the same project that wrote the detector. "
                   "These numbers verify behaviour against documented assumptions; they are not "
                   "evidence of real-world accuracy and say nothing about failure prediction."),
        "profile": profile.name,
        "split": profile.split,
        "seeds": list(profile.seeds),
        "reference_days": profile.reference_days,
        "eval_days": profile.eval_days,
        "vehicles_per_scenario": 8,
        "detector_config": BASE.to_dict(),
        "detectors": DETECTOR_NOTES,
        "criteria": {"coverage_floor": COVERAGE_FLOOR, "non_inferiority_margin": NON_INFERIORITY_MARGIN},
        "scenarios": {
            scenario: {"description": SCENARIOS[scenario].description,
                       "results": {d: metrics(totals[(scenario, d)]) for d in DETECTORS}}
            for scenario in names
        },
    }
    results["hypotheses"] = evaluate_hypotheses(results["scenarios"])
    seconds["metrics and bootstrap"] = time.perf_counter() - t0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    uname = os.uname()  # platform.platform() would spawn `uname -p`
    provenance = {
        "pexislink_version": __version__,
        "code_sha256": code_fingerprint(),
        "python": sys.version.split()[0],
        "implementation": sys.implementation.name,
        "platform": f"{uname.sysname}-{uname.release}-{uname.machine}",
        "wall_time_s": round(time.perf_counter() - started, 2),
        "cpu_time_s": round(usage.ru_utime + usage.ru_stime, 2),
        "peak_rss_mb": round(usage.ru_maxrss / 1024, 1),  # Linux reports KiB
        "seconds_by_stage": {k: round(v, 2) for k, v in seconds.items()},
        "rows_per_second": {k: round(rows_processed[k] / seconds[k]) for k in DETECTORS if seconds[k] > 0},
    }
    return results, provenance


def code_fingerprint() -> str:
    """SHA-256 over the package sources, for provenance (not part of results)."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


# ------------------------------------------------------------ hypotheses

def evaluate_hypotheses(scenarios: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Mechanical verdicts. Criteria were tightened after independent review
    (ADR 0001 §7): every "fewer errors" claim also needs coverage >= COVERAGE_FLOOR,
    and detection claims use direction-aware, horizon-bounded excess recall with a
    bootstrap interval. The original H2 criterion is still reported."""
    def m(scenario: str, detector: str) -> dict[str, Any]:
        results: dict[str, Any] = scenarios.get(scenario, {}).get("results", {})
        found: dict[str, Any] = results.get(detector, {})
        return found

    def upper_fa(r: dict[str, Any]) -> float | None:
        if r.get("false_alerts") == 0:
            bound: float | None = r.get("false_alerts_zero_count_upper_per_1000h")
            return bound
        interval = r.get("false_alerts_per_1000h_ci95")
        return interval[1] if interval else None

    def lower_fa(r: dict[str, Any]) -> float | None:
        interval = r.get("false_alerts_per_1000h_ci95")
        return interval[0] if interval else None

    def fewer(a: dict[str, Any], b: dict[str, Any]) -> bool:
        high, low = upper_fa(a), lower_fa(b)
        return high is not None and low is not None and high < low

    def fewer_trips(a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Share of trips with a false alert: an always-alarming detector cannot win this."""
        high, low = a.get("trips_with_false_alert_ci95"), b.get("trips_with_false_alert_ci95")
        return bool(high and low and high[1] < low[0])

    def covered(r: dict[str, Any]) -> bool:
        return (r.get("coverage") or 0) >= COVERAGE_FLOOR

    def detects(a: dict[str, Any], b: dict[str, Any]) -> str:
        """'supported' if a's excess recall is demonstrably > 0 and not worse than b's by more than the margin."""
        interval = a.get("excess_recall_ci95")
        if a.get("excess_recall") is None or not interval:
            return "not applicable"
        if interval[0] <= 0:
            return "not supported (excess recall interval includes 0)"
        if a["excess_recall"] < (b.get("excess_recall") or 0) - NON_INFERIORITY_MARGIN:
            return "not supported (inferior to comparator)"
        return "supported"

    def fa_text(r: dict[str, Any]) -> str:
        if r.get("false_alerts") == 0:
            return f"0 (<= {r.get('false_alerts_zero_count_upper_per_1000h')} if independent)"
        return f"{r.get('false_alerts_per_1000h')} {r.get('false_alerts_per_1000h_ci95')}"

    out: dict[str, dict[str, str]] = {}
    if "normal_operation" in scenarios:
        v2, v1 = m("normal_operation", "v0.2"), m("normal_operation", "v0.1-cleaned")
        verdict = ("not supported (coverage below floor)" if not covered(v2) else
                   "supported" if fewer(v2, v1) and fewer_trips(v2, v1) else "inconclusive")
        out["H1"] = {"claim": "v0.2 raises fewer false alerts than v0.1 on normal operation (per eligible hour and "
                              "per trip), with coverage >= floor",
                     "evidence": (f"false alerts per 1000 eligible h: v0.2 {fa_text(v2)} vs v0.1 {fa_text(v1)}; "
                                  f"trips with a false alert: v0.2 {v2.get('trips_with_false_alert')} vs v0.1 "
                                  f"{v1.get('trips_with_false_alert')}; v0.2 coverage {v2.get('coverage')}"),
                     "verdict": verdict}
    if "abrupt_anomaly" in scenarios and "gradual_sensor_drift" in scenarios:
        a2, a1 = m("abrupt_anomaly", "v0.2"), m("abrupt_anomaly", "v0.1-cleaned")
        d2, d1 = m("gradual_sensor_drift", "v0.2"), m("gradual_sensor_drift", "v0.1-cleaned")
        for key, label, v2, v1 in (("H2a", "abrupt anomalies (24 h horizon)", a2, a1),
                                   ("H2b", "gradual drift", d2, d1)):
            out[key] = {"claim": f"v0.2 demonstrably detects {label}, not worse than v0.1 by more than "
                                 f"{NON_INFERIORITY_MARGIN} excess recall (direction-aware, placebo-controlled)",
                        "evidence": (f"v0.2 recall {v2.get('recall')} placebo {v2.get('placebo_recall')} "
                                     f"excess {v2.get('excess_recall')} {v2.get('excess_recall_ci95')}; "
                                     f"v0.1 recall {v1.get('recall')} placebo {v1.get('placebo_recall')} "
                                     f"excess {v1.get('excess_recall')} {v1.get('excess_recall_ci95')}"),
                        "verdict": detects(v2, v1)}
        b2, b1 = d2.get("median_drift_bias_at_detection_c"), d1.get("median_drift_bias_at_detection_c")
        original = ((a2.get("recall") or 0) >= (a1.get("recall") or 0)
                    and b2 is not None and (b1 is None or b2 < b1))
        out["H2-original"] = {"claim": "as first pre-registered: v0.2 abrupt recall >= v0.1 and drift detected at a "
                                       "smaller bias (raw recall; no placebo control)",
                              "evidence": (f"abrupt recall v0.2 {a2.get('recall')} vs v0.1 {a1.get('recall')}; "
                                           f"median bias at detection v0.2 {b2} C vs v0.1 {b1} C"),
                              "verdict": "supported" if original else "not supported"}
    if "load_change" in scenarios:
        v2, no_load = m("load_change", "v0.2"), m("load_change", "v0.2-no-load")
        towing = v2.get("conditions", {}).get("load_increase", {}).get("coverage")
        separated = (no_load.get("false_alerts") or 0) > 0 and fewer(v2, no_load)
        verdict = ("not supported" if not separated else
                   "supported" if (towing or 0) >= COVERAGE_FLOOR else
                   f"supported only by abstention (coverage on towing rows {towing})")
        out["H3"] = {"claim": "without a load signal, load increases cause false alerts; with it v0.2 avoids "
                              "them while still assessing the affected rows",
                     "evidence": (f"false alerts per 1000 eligible h: v0.2 {fa_text(v2)}, v0.2-no-load "
                                  f"{fa_text(no_load)}; v0.2 coverage on towing rows {towing}, v0.2-no-load "
                                  f"{no_load.get('conditions', {}).get('load_increase', {}).get('coverage')}"),
                     "verdict": verdict}
    if "unreliable_sensor" in scenarios:
        v2, v1 = m("unreliable_sensor", "v0.2"), m("unreliable_sensor", "v0.1-cleaned")
        no_stuck = m("unreliable_sensor", "v0.2-no-stuck")
        f2, f1 = v2.get("false_reassurance_ci95"), v1.get("false_reassurance_ci95")
        verdict = ("not supported (coverage below floor)" if not covered(v2) else
                   "supported" if f2 and f1 and f2[1] < f1[0] and fewer_trips(v2, v1) else "inconclusive")
        flicker = m("stress_stuck_flicker", "v0.2").get("false_reassurance") if "stress_stuck_flicker" in scenarios else None
        out["H4"] = {"claim": "v0.2 reduces false reassurance ('normal' while the sensor is frozen) without more "
                              "trips with false alerts, with coverage >= floor",
                     "evidence": (f"false reassurance v0.2 {v2.get('false_reassurance')} {f2} vs v0.1 "
                                  f"{v1.get('false_reassurance')} {f1}; stuck rule off {no_stuck.get('false_reassurance')}; "
                                  f"frozen sensor with 1-LSB flicker: v0.2 {flicker}"),
                     "verdict": verdict}
    if "unseen_vehicles_and_regimes" in scenarios:
        v2 = m("unseen_vehicles_and_regimes", "v0.2")
        v1 = m("unseen_vehicles_and_regimes", "v0.1-cleaned")
        n = v2.get("normal_rows_on_unseen_vehicles")
        out["H5"] = {"claim": "unseen vehicles: v0.2 never reports 'normal' (a design property, true by construction)",
                     "evidence": f"normal rows on vehicles without reference: v0.2 {n}, v0.1 {v1.get('normal_rows_on_unseen_vehicles')}",
                     "verdict": "holds (by construction)" if n == 0 else "violated"}
    if "seasonal_shift" in scenarios and "normal_operation" in scenarios:
        s, n = m("seasonal_shift", "v0.2"), m("normal_operation", "v0.2")
        cold = s.get("conditions", {}).get("cold_season", {})
        hot = s.get("conditions", {}).get("hot_season", {})
        baseline = upper_fa(n)
        observed = (cold.get("false_alerts_per_1000h") or 0) > (baseline if baseline is not None else float("inf"))
        out["H6"] = {"claim": "(expected negative) severe cold outlasts warmup_s and raises v0.2 false alerts",
                     "evidence": (f"v0.2 false alerts per 1000 eligible h: cold season {cold.get('false_alerts_per_1000h')} "
                                  f"({cold.get('false_alerts')} alerts), hot season {hot.get('false_alerts_per_1000h')} "
                                  f"({hot.get('false_alerts')} alerts), normal operation {fa_text(n)}"),
                     "verdict": "observed" if observed else "not observed"}
    if "contaminated_reference" in scenarios:
        no_abs, v2 = m("contaminated_reference", "v0.2-no-absolute"), m("contaminated_reference", "v0.2")
        full = no_abs.get("recall_by_kind", {}).get("overheat_in_reference_full", "")
        out["H7"] = {"claim": "(expected negative) a fault present during the whole reference period is missed by the relative barrier",
                     "evidence": (f"relative barrier only (v0.2-no-absolute): {no_abs.get('recall_by_kind')}; "
                                  f"with the absolute barrier (v0.2): {v2.get('recall_by_kind')}"),
                     "verdict": "observed" if full.startswith("0/") else "not observed"}
    return out


# ------------------------------------------------------------------- report

def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)


def _ci(value: Any, interval: Any) -> str:
    if value is None:
        return "N/A"
    return f"{_fmt(value)} [{_fmt(interval[0])}, {_fmt(interval[1])}]" if interval else _fmt(value)


def _false_alerts(r: dict[str, Any]) -> str:
    if r["false_alerts"] == 0 and r["false_alerts_zero_count_upper_per_1000h"] is not None:
        return f"0 (≤ {_fmt(r['false_alerts_zero_count_upper_per_1000h'])} if independent)"
    return _ci(r["false_alerts_per_1000h"], r["false_alerts_per_1000h_ci95"])


def render_markdown(results: dict[str, Any], provenance: dict[str, Any]) -> str:
    criteria = results["criteria"]
    lines = [
        f"# PexisLink benchmark ({results['profile']} profile, {results['split']} split)",
        "",
        f"> {results['notice']}",
        "",
        f"- Seeds: {results['seeds']}; {results['vehicles_per_scenario']} vehicles per scenario; "
        f"{results['reference_days']} reference days then {results['eval_days']} evaluation days per vehicle.",
        f"- pexislink {provenance['pexislink_version']}, Python {provenance['python']}, code sha256 "
        f"`{provenance['code_sha256'][:16]}`.",
        "- 95 % intervals: bootstrap over vehicles (1000 resamples, fixed seed). For zero false alerts the "
        "exact Poisson bound is shown, which assumes independent alerts. N/A means not applicable, never zero.",
        "- Detection is direction-aware: overheat must be reported *above* reference, stuck-open thermostat "
        "*below*, drift in the sign of its rate. Abrupt faults count only if detected within 24 h of onset.",
        "- **Placebo recall**: the same windows and directions on fault-free control vehicles; "
        "excess recall = recall - placebo recall. A detection claim needs excess recall clearly above 0.",
        "- False alerts are counted per 1000 *eligible* operating hours (outside fault intervals) and per "
        "eligible trip. Misdirected alerts (inside a fault interval, wrong direction) are counted separately.",
        f"- Hypothesis criteria: coverage floor {criteria['coverage_floor']}, non-inferiority margin "
        f"{criteria['non_inferiority_margin']} on excess recall.",
        "",
        "## Hypotheses (verdicts computed mechanically)",
        "",
        "| | Claim | Evidence | Verdict |",
        "|---|---|---|---|",
    ]
    for key, h in results["hypotheses"].items():
        lines.append(f"| {key} | {h['claim']} | {h['evidence']} | **{h['verdict']}** |")
    lines += ["", "## Detectors", ""]
    lines += [f"- `{name}`: {note}" for name, note in results["detectors"].items()]
    for scenario, block in results["scenarios"].items():
        lines += ["", f"## {scenario}", "", block["description"], "",
                  "| Detector | Recall [95% CI] | Placebo | Excess recall [95% CI] | Missed w/o evidence "
                  "| Alerts true/misdirected/false | Precision | False alerts per 1000 eligible h "
                  "| Trips with false alert | Median delay, operating min | Coverage | Notes |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for detector, r in block["results"].items():
            if r["failed_runs"] == r["runs"]:
                lines.append(f"| {detector} | run failed on {r['failed_runs']}/{r['runs']} seeds | | | | | | | | | | |")
                continue
            notes = []
            if r["failed_runs"]:
                notes.append(f"failed {r['failed_runs']}/{r['runs']} runs")
            if r["false_reassurance"] is not None:
                notes.append(f"false reassurance {_ci(r['false_reassurance'], r['false_reassurance_ci95'])}")
            if r["median_drift_bias_at_detection_c"] is not None:
                notes.append(f"bias at detection {_fmt(r['median_drift_bias_at_detection_c'])} C")
            if r["normal_rows_on_unseen_vehicles"] is not None:
                notes.append(f"normal rows on unseen vehicles {r['normal_rows_on_unseen_vehicles']}")
            if len(r["recall_by_kind"]) > 1:
                notes.append("recall by kind " + ", ".join(f"{k} {v}" for k, v in r["recall_by_kind"].items()))
            conditions = [f"{k}: coverage {_fmt(c['coverage'])}, {c['false_alerts']} false alerts"
                          for k, c in r["conditions"].items()]
            if conditions:
                notes.append("; ".join(conditions))
            delays = ", ".join(f"{k} {_fmt(d['median_operating_min'])} (n={d['n']}, max {_fmt(d['max_operating_min'])})"
                               for k, d in r["delays_by_kind"].items()) or "N/A"
            lines.append(
                f"| {detector} | {_ci(r['recall'], r['recall_ci95'])} | {_fmt(r['placebo_recall'])} "
                f"| {_ci(r['excess_recall'], r['excess_recall_ci95'])} | {_fmt(r['missed_without_evidence'])} "
                f"| {r['true_alerts']}/{r['misdirected_alerts']}/{r['false_alerts']} | {_fmt(r['precision'])} "
                f"| {_false_alerts(r)} | {_fmt(r['trips_with_false_alert'])} | {delays} "
                f"| {_fmt(r['coverage'])} | {'; '.join(notes)} |")
    lines += ["", "## Runtime (this machine; not part of the deterministic results)", "",
              f"- Wall time {provenance['wall_time_s']} s, CPU time {provenance['cpu_time_s']} s, "
              f"peak RSS {provenance['peak_rss_mb']} MB on {provenance['platform']}.",
              "", "| Stage | Seconds | Rows per second |", "|---|---|---|"]
    for stage, value in provenance["seconds_by_stage"].items():
        lines.append(f"| {stage} | {value} | {_fmt(provenance['rows_per_second'].get(stage))} |")
    return "\n".join(lines) + "\n"


def cli_benchmark(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink benchmark", description="Run the offline synthetic benchmark.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="quick")
    parser.add_argument("--scenarios", help="comma-separated subset (default: all)")
    parser.add_argument("--out", required=True, help="folder for results.json, provenance.json, report.md")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(list(argv))
    scenarios = args.scenarios.split(",") if args.scenarios else None
    unknown = sorted(set(scenarios or ()) - set(SCENARIOS))
    if unknown:
        print(f"error: unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    progress = None if args.quiet else (lambda message: print(message, file=sys.stderr))
    results, provenance = run_benchmark(PROFILES[args.profile], scenarios, progress)
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True, allow_nan=False) + "\n",
                                         encoding="utf-8")
    (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    (folder / "report.md").write_text(render_markdown(results, provenance), encoding="utf-8")
    for key, h in results["hypotheses"].items():
        print(f"{key}: {h['verdict']}")
    return 0
