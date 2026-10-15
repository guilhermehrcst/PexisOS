"""Evaluation harness: exact statistics, hand-computed scoring, and validation of
the metrics themselves with oracle / null / always-alarm / mislabelled detectors."""

import unittest

from pexislink.evaluate import Score, metrics, poisson_interval, quantile, score, wilson
from pexislink.synth import Event, GroundTruth, generate

MIN = 60_000_000


def truth(rows, events, unseen=()):
    vehicles, epochs = zip(*rows)
    return GroundTruth(tuple(events), vehicles, epochs, frozenset(unseen), 0, 10**12)


class StatisticsTests(unittest.TestCase):
    def test_wilson_reference_values(self):
        for (k, n), (lo, hi) in {(8, 10): (0.4902, 0.9433), (0, 10): (0.0, 0.2775),
                                 (10, 10): (0.7225, 1.0), (1, 2): (0.0945, 0.9055)}.items():
            got = wilson(k, n)
            self.assertAlmostEqual(got[0], lo, places=4)
            self.assertAlmostEqual(got[1], hi, places=4)
        self.assertIsNone(wilson(0, 0))

    def test_exact_poisson_reference_values(self):
        # Garwood intervals (chi-square quantiles), 95 %. The k=100 bounds were
        # re-derived with 50-digit decimal arithmetic (P(X<=99; 81.36399) = 0.975).
        for k, (lo, hi) in {0: (0.0, 3.6889), 1: (0.0253, 5.5716), 5: (1.6235, 11.6683),
                            10: (4.7954, 18.3904), 100: (81.3640, 121.6268)}.items():
            got = poisson_interval(k)
            self.assertAlmostEqual(got[0], lo, places=3)
            self.assertAlmostEqual(got[1], hi, places=3)

    def test_nearest_rank_quantile(self):
        self.assertEqual(quantile(list(range(1, 11)), 0.9), 9.0)
        self.assertEqual(quantile(list(range(1, 31)), 0.1), 3.0)
        self.assertIsNone(quantile([], 0.5))


class HandComputedScoringTests(unittest.TestCase):
    def test_alerts_detection_delay_exposure_and_reassurance(self):
        rows = [("a", i * MIN) for i in range(10)] + [("b", i * MIN) for i in range(6)]
        events = [Event("a", "overheat", "anomaly", 3 * MIN, 9 * MIN),
                  Event("b", "sensor_stuck", "sensor_fault", 2 * MIN, 5 * MIN),
                  Event("b", "load_increase", "nuisance", 0, 5 * MIN)]
        statuses = (["normal", "confirmed_anomaly", "normal", "insufficient_evidence",
                     "suspected_anomaly", "confirmed_anomaly", "insufficient_evidence",
                     "confirmed_anomaly", "normal", "confirmed_anomaly"]
                    + ["normal", "confirmed_anomaly", "normal", "normal", "insufficient_evidence", "normal"])
        s = score(statuses, truth(rows, events, unseen={"b"}))
        self.assertEqual((s.alerts, s.true_alerts, s.false_alerts, s.false_alerts_in_nuisance), (4, 2, 2, 1))
        self.assertEqual((s.events, s.detected), (1, 1))
        self.assertEqual(s.delays_wall_us, [2 * MIN])
        self.assertEqual(s.delays_operating_us, [2 * MIN])
        self.assertEqual(s.exposure_us, 9 * MIN + 5 * MIN)
        self.assertEqual((s.fault_rows, s.fault_rows_normal), (4, 3))
        self.assertEqual((s.unseen_rows, s.unseen_rows_normal), (6, 4))
        self.assertEqual(s.assessable, 13)

    def test_segment_gap_splits_alerts_and_excludes_exposure(self):
        rows = [("a", 0), ("a", MIN), ("a", 100 * MIN), ("a", 101 * MIN)]
        s = score(["confirmed_anomaly"] * 4, truth(rows, []))
        self.assertEqual((s.alerts, s.false_alerts), (2, 2))
        self.assertEqual(s.exposure_us, 2 * MIN)

    def test_failed_run_and_alignment(self):
        self.assertEqual(score(None, truth([("a", 0)], [])).failed_runs, 1)
        with self.assertRaises(ValueError):
            score(["normal"], truth([("a", 0), ("a", 1)], []))

    def test_not_applicable_metrics_are_none(self):
        m = metrics(score(["insufficient_evidence"], truth([("a", 0)], [])))
        for key in ("recall", "precision", "median_delay_operating_min", "false_reassurance",
                    "median_drift_bias_at_detection_c", "normal_rows_on_unseen_vehicles"):
            self.assertIsNone(m[key], key)

    def test_pooling_is_additive(self):
        rows = [("a", 0), ("a", MIN)]
        one = score(["confirmed_anomaly", "normal"], truth(rows, []))
        total = Score()
        total.add(one)
        total.add(one)
        self.assertEqual((total.runs, total.alerts, total.exposure_us), (2, 2, 2 * MIN))


class HarnessValidationTests(unittest.TestCase):
    """Detectors with known answers prove the metrics are not trivially satisfied."""

    @classmethod
    def setUpClass(cls):
        cls.data = generate("abrupt_anomaly", 7, reference_days=6, eval_days=14)

    def labelled(self, events):
        windows = {}
        for e in events:
            if e.category in ("anomaly", "sensor_fault"):
                windows.setdefault(e.vehicle_id, []).append((e.onset_us, e.end_us))
        return ["confirmed_anomaly" if any(a <= t <= b for a, b in windows.get(v, ())) else "normal"
                for v, t in zip(self.data.truth.observation_vehicles, self.data.truth.observation_epochs_us)]

    def test_oracle_null_and_always_alarm(self):
        oracle = metrics(score(self.labelled(self.data.truth.events), self.data.truth))
        self.assertEqual((oracle["recall"], oracle["precision"], oracle["false_alerts"]), (1.0, 1.0, 0))
        null = metrics(score(["insufficient_evidence"] * len(self.data.observation_rows), self.data.truth))
        self.assertEqual((null["recall"], null["alerts"], null["coverage"], null["precision"]), (0.0, 0, 0.0, None))
        self.assertEqual(null["missed_without_evidence"], 5)
        always = metrics(score(["confirmed_anomaly"] * len(self.data.observation_rows), self.data.truth))
        self.assertEqual(always["recall"], 1.0)
        self.assertGreater(always["false_alerts"], 50)
        self.assertLess(always["precision"], 0.5)

    def test_placebo_control_exposes_coincidental_detection(self):
        oracle = metrics(score(self.labelled(self.data.truth.events), self.data.truth))
        self.assertEqual((oracle["placebo_recall"], oracle["excess_recall"]), (0.0, 1.0))
        always = metrics(score(["confirmed_anomaly"] * len(self.data.observation_rows), self.data.truth))
        self.assertEqual((always["recall"], always["placebo_recall"], always["excess_recall"]), (1.0, 1.0, 0.0))
        self.assertEqual(always["placebo_recall_ci95"][1], 1.0)
        # 5 anomaly events x 3 control vehicles (veh-05..07)
        self.assertEqual(score(["normal"] * len(self.data.observation_rows), self.data.truth).placebo_events, 15)

    def test_mislabelled_oracle_collapses(self):
        """Permutation control: labels moved to other vehicles must not score well."""
        shifted = [Event(f"veh-{(int(e.vehicle_id[-2:]) + 5) % 8:02d}", e.kind, e.category,
                         e.onset_us, e.end_us, e.params) for e in self.data.truth.events]
        wrong = metrics(score(self.labelled(shifted), self.data.truth))
        self.assertLess(wrong["recall"], 0.5)
        self.assertLess(wrong["precision"], 0.5)


if __name__ == "__main__":
    unittest.main()
