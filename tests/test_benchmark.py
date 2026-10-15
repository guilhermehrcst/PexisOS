"""Benchmark runner: determinism, comparators, hypotheses and report rendering."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pexislink.baselines import fixed_limit, legacy_v01
from pexislink.benchmark import PROFILES, Profile, evaluate_hypotheses, render_markdown, run_benchmark
from pexislink.cli import main
from pexislink.evaluate import metrics, score
from pexislink.synth import generate

TINY = Profile("tiny", "dev", (101,), 4, 13)


class ComparatorTests(unittest.TestCase):
    def test_v01_as_shipped_fails_where_v01_rejects_rows_and_cleaned_does_not(self):
        data = generate("data_integrity", 101, reference_days=4, eval_days=13)
        self.assertIsNone(legacy_v01(data.reference_rows, data.observation_rows, cleaned=False))
        cleaned = legacy_v01(data.reference_rows, data.observation_rows, cleaned=True)
        self.assertEqual(len(cleaned), len(data.observation_rows))
        self.assertIn(("insufficient_evidence", 0), cleaned)  # dropped duplicates are explicit

    def test_cleaning_is_a_no_op_on_clean_data(self):
        data = generate("normal_operation", 101, reference_days=4, eval_days=13)
        self.assertEqual(legacy_v01(data.reference_rows, data.observation_rows, cleaned=False),
                         legacy_v01(data.reference_rows, data.observation_rows, cleaned=True))

    def test_v01_direction_follows_its_delta(self):
        data = generate("normal_operation", 101, reference_days=4, eval_days=13)
        verdicts = legacy_v01(data.reference_rows, data.observation_rows, cleaned=True)
        self.assertTrue({d for s, d in verdicts if s == "confirmed_anomaly"} <= {1, -1})
        self.assertIn(-1, {d for s, d in verdicts})  # cold starts read below reference

    def test_fixed_limit_rule(self):
        rows = [(f"2026-10-02T08:0{i}:00Z", "v", "800", "0", c, "10")
                for i, c in enumerate(("90", "116", "117", "90", "", "120"))]
        self.assertEqual(fixed_limit(rows), [("normal", 0), ("suspected_anomaly", 1), ("confirmed_anomaly", 1),
                                             ("normal", 0), ("insufficient_evidence", 0),
                                             ("suspected_anomaly", 1)])


class RunnerTests(unittest.TestCase):
    def test_results_are_deterministic_and_complete(self):
        scenarios = ["normal_operation", "abrupt_anomaly"]
        first, provenance = run_benchmark(TINY, scenarios)
        second, _ = run_benchmark(TINY, scenarios)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(set(first["scenarios"]), set(scenarios))
        self.assertIn("H1", first["hypotheses"])
        self.assertEqual(len(provenance["code_sha256"]), 64)
        report = render_markdown(first, provenance)
        self.assertIn("not evidence of real-world accuracy", report)
        self.assertIn("Placebo", report)

    def test_seed_splits_are_disjoint(self):
        dev = set(PROFILES["dev"].seeds) | set(PROFILES["quick"].seeds)
        self.assertTrue(dev.isdisjoint(PROFILES["full"].seeds))
        self.assertEqual(PROFILES["full"].split, "test")


class HypothesisCriteriaTests(unittest.TestCase):
    """Scientific review F1: a detector that never outputs anything, or one that
    always alarms, must not be able to support any detection or safety claim."""

    @classmethod
    def setUpClass(cls):
        cls.verdicts = {}
        built: dict[str, dict] = {"null": {}, "always": {}}
        for name in ("normal_operation", "abrupt_anomaly", "gradual_sensor_drift", "load_change",
                     "unreliable_sensor"):
            data = generate(name, 101, reference_days=4, eval_days=13)
            n = len(data.observation_rows)
            v01 = metrics(score(legacy_v01(data.reference_rows, data.observation_rows, cleaned=True), data.truth))
            for label, verdicts in (("null", [("insufficient_evidence", 0)] * n),
                                    ("always", [("confirmed_anomaly", 1)] * n)):
                m = metrics(score(verdicts, data.truth))
                built[label][name] = {"results": {"v0.2": m, "v0.2-no-load": m, "v0.2-no-stuck": m,
                                                  "v0.1-cleaned": v01}}
        for label, scenarios in built.items():
            cls.verdicts[label] = {k: h["verdict"] for k, h in evaluate_hypotheses(scenarios).items()}

    def test_degenerate_detectors_support_nothing(self):
        for label, verdicts in self.verdicts.items():
            for key in ("H1", "H2a", "H2b", "H3", "H4"):
                with self.subTest(detector=label, hypothesis=key):
                    self.assertFalse(verdicts[key].startswith("supported"), verdicts[key])

    def test_inconclusive_when_intervals_overlap(self):
        def block(fa, ci, trips_ci, coverage=0.5):
            return {"false_alerts": fa, "false_alerts_per_1000h": fa, "false_alerts_per_1000h_ci95": ci,
                    "trips_with_false_alert_ci95": trips_ci, "coverage": coverage}
        scenarios = {"normal_operation": {"results": {"v0.2": block(5, [3.0, 9.0], [0.1, 0.3]),
                                                      "v0.1-cleaned": block(6, [4.0, 10.0], [0.2, 0.4])}}}
        self.assertEqual(evaluate_hypotheses(scenarios)["H1"]["verdict"], "inconclusive")
        scenarios["normal_operation"]["results"]["v0.1-cleaned"] = block(50, [40.0, 60.0], [0.9, 1.0])
        self.assertEqual(evaluate_hypotheses(scenarios)["H1"]["verdict"], "supported")


class CommandTests(unittest.TestCase):
    def test_benchmark_and_synth_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["synth", "--scenario", "abrupt_anomaly", "--seed", "3", "--out", folder,
                             "--reference-days", "4", "--eval-days", "13"])
            self.assertEqual(code, 0)
            truth = json.loads((Path(folder) / "truth.json").read_text())
            self.assertIn("Never feed it to a detector", truth["notice"])
            header = (Path(folder) / "observations.csv").read_text().splitlines()[0]
            self.assertEqual(header, "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c,engine_load_pct")
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["detect", "--reference", f"{folder}/reference.csv",
                             "--observations", f"{folder}/observations.csv"])
            self.assertEqual(code, 0)
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(main(["benchmark", "--scenarios", "nope", "--out", folder]), 2)


if __name__ == "__main__":
    unittest.main()
