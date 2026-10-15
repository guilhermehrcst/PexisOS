"""Evaluation harness: exact statistics, hand-computed scoring, and validation of
the metrics themselves with oracle / null / always-alarm / mislabelled detectors."""

import unittest

from pexislink.evaluate import (Score, bootstrap_intervals, metrics, poisson_interval, quantile,
                                score)
from pexislink.synth import DAY_US, Event, GroundTruth, generate

MIN = 60_000_000
HOUR = 60 * MIN
N = ("normal", 0)
INS = ("insufficient_evidence", 0)
UP, DOWN = ("confirmed_anomaly", 1), ("confirmed_anomaly", -1)
SUSPECT = ("suspected_anomaly", 1)


def truth(rows, events, unseen=()):
    vehicles, epochs = zip(*rows)
    return GroundTruth(tuple(events), vehicles, epochs, frozenset(unseen), 0, 10**12)


class StatisticsTests(unittest.TestCase):
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

    def test_bootstrap_is_deterministic_and_handles_degenerate_input(self):
        data = generate("abrupt_anomaly", 7, reference_days=4, eval_days=13)
        s = score([N] * len(data.observation_rows), data.truth)
        self.assertEqual(bootstrap_intervals(s.vehicles), bootstrap_intervals(s.vehicles))
        self.assertEqual(bootstrap_intervals(s.vehicles)["false_alerts_per_1000h"], [0.0, 0.0])
        self.assertIsNone(bootstrap_intervals([])["recall"])


class HandComputedScoringTests(unittest.TestCase):
    def setUp(self):
        rows = ([("a", i * MIN) for i in range(10)] + [("b", i * MIN) for i in range(6)]
                + [("c", i * MIN) for i in range(10)])
        self.events = [Event("a", "overheat", "anomaly", 3 * MIN, 9 * MIN, direction=1, horizon_us=DAY_US),
                       Event("b", "sensor_stuck", "sensor_fault", 2 * MIN, 5 * MIN),
                       Event("b", "load_increase", "nuisance", 0, 5 * MIN)]
        verdicts = ([N, UP, N, INS, SUSPECT, UP, INS, UP, N, DOWN]   # a
                    + [N, UP, N, N, INS, N]                         # b
                    + [N, N, N, N, UP, N, N, N, N, N])            # c (control)
        self.score = score(verdicts, truth(rows, self.events, unseen={"b"}))
        self.tally = dict(zip("abc", self.score.vehicles))

    def test_alert_attribution_with_direction(self):
        a, b, c = self.tally["a"], self.tally["b"], self.tally["c"]
        # a: minute 1 before onset = false; minute 5 after a suspected row = new, true;
        # minute 7 continues it (insufficient rows neither end nor extend); minute 9
        # changes direction inside the fault window = misdirected.
        self.assertEqual((a.alerts, a.false_alerts, a.true_alerts, a.misdirected_alerts), (3, 1, 1, 1))
        self.assertEqual((b.alerts, b.false_alerts), (1, 1))
        self.assertEqual((c.alerts, c.false_alerts), (1, 1))
        self.assertEqual(self.score.conditions["load_increase"][2], 1)  # b's false alert, in the nuisance window

    def test_detection_delay_and_placebo(self):
        a, c = self.tally["a"], self.tally["c"]
        self.assertEqual((a.events, a.detected), (1, 1))
        self.assertEqual(self.score.delays_wall_us["overheat"], [2 * MIN])
        self.assertEqual(self.score.delays_operating_us["overheat"], [2 * MIN])
        self.assertEqual((c.placebo_events, c.placebo_detected), (1, 1))  # c alarms upwards at minute 4

    def test_exposure_trips_reassurance_and_coverage(self):
        a, b, c = self.tally["a"], self.tally["b"], self.tally["c"]
        self.assertEqual((a.exposure_us, a.eligible_exposure_us), (9 * MIN, 2 * MIN))
        self.assertEqual((b.exposure_us, b.eligible_exposure_us), (5 * MIN, 1 * MIN))
        self.assertEqual(c.eligible_exposure_us, 9 * MIN)
        self.assertEqual([(t.eligible_trips, t.trips_with_false_alert) for t in (a, b, c)], [(1, 1)] * 3)
        self.assertEqual((b.fault_rows, b.fault_rows_normal), (4, 3))
        self.assertEqual((b.unseen_rows, b.unseen_rows_normal), (6, 4))
        self.assertEqual((a.rows, a.assessable), (10, 8))

    def test_horizon_and_direction_are_enforced(self):
        rows = [("a", 0), ("a", 2 * HOUR)]
        late = Event("a", "overheat", "anomaly", 0, 10 * HOUR, direction=1, horizon_us=HOUR)
        self.assertEqual(score([N, UP], truth(rows, [late])).vehicles[0].detected, 0)
        cold = Event("a", "thermostat_stuck_open", "anomaly", 0, 10 * HOUR, direction=-1, horizon_us=None)
        tally = score([UP, UP], truth(rows, [cold])).vehicles[0]  # 2 h apart: two trips, two alerts
        self.assertEqual((tally.detected, tally.misdirected_alerts), (0, 2))

    def test_segment_gap_splits_alerts_and_excludes_exposure(self):
        rows = [("a", 0), ("a", MIN), ("a", 100 * MIN), ("a", 101 * MIN)]
        tally = score([UP] * 4, truth(rows, [])).vehicles[0]
        self.assertEqual((tally.alerts, tally.false_alerts, tally.eligible_trips), (2, 2, 2))
        self.assertEqual(tally.exposure_us, 2 * MIN)

    def test_failed_run_and_alignment(self):
        self.assertEqual(score(None, truth([("a", 0)], [])).failed_runs, 1)
        with self.assertRaises(ValueError):
            score([N], truth([("a", 0), ("a", 1)], []))

    def test_not_applicable_metrics_are_none(self):
        m = metrics(score([INS], truth([("a", 0)], [])))
        for key in ("recall", "precision", "false_reassurance", "median_drift_bias_at_detection_c",
                    "normal_rows_on_unseen_vehicles", "excess_recall"):
            self.assertIsNone(m[key], key)

    def test_pooling_is_additive(self):
        rows = [("a", 0), ("a", MIN)]
        one = score([UP, N], truth(rows, []))
        total = Score()
        total.add(one)
        total.add(one)
        m = metrics(total)
        self.assertEqual((m["runs"], m["alerts"], m["vehicles"]), (2, 2, 2))


class HarnessValidationTests(unittest.TestCase):
    """Detectors with known answers prove the metrics are not trivially satisfied."""

    @classmethod
    def setUpClass(cls):
        cls.data = generate("abrupt_anomaly", 7, reference_days=6, eval_days=14)

    def oracle(self, events):
        windows = {}
        for e in events:
            if e.category in ("anomaly", "sensor_fault"):
                windows.setdefault(e.vehicle_id, []).append((e.onset_us, e.end_us, e.direction or 1))
        out = []
        for v, t in zip(self.data.truth.observation_vehicles, self.data.truth.observation_epochs_us):
            hit = next((d for a, b, d in windows.get(v, ()) if a <= t <= b), None)
            out.append(("confirmed_anomaly", hit) if hit else N)
        return out

    def test_oracle_null_and_always_alarm(self):
        n = len(self.data.observation_rows)
        oracle = metrics(score(self.oracle(self.data.truth.events), self.data.truth))
        self.assertEqual((oracle["recall"], oracle["placebo_recall"], oracle["excess_recall"],
                          oracle["false_alerts"], oracle["misdirected_alerts"]), (1.0, 0.0, 1.0, 0, 0))
        self.assertGreater(oracle["excess_recall_ci95"][0], 0)
        null = metrics(score([INS] * n, self.data.truth))
        self.assertEqual((null["recall"], null["excess_recall"], null["alerts"], null["coverage"]), (0.0, 0.0, 0, 0.0))
        self.assertEqual(null["missed_without_evidence"], 5)
        always = metrics(score([UP] * n, self.data.truth))
        # alarming upwards everywhere "detects" the overheats but not the cold thermostats,
        # and detects the placebos exactly as often: excess recall 0
        self.assertEqual((always["recall"], always["placebo_recall"], always["excess_recall"]), (0.6, 0.6, 0.0))
        self.assertEqual(always["trips_with_false_alert"], 1.0)

    def test_mislabelled_oracle_collapses(self):
        """Permutation control: labels moved to other vehicles must not score well."""
        faulty = sorted({e.vehicle_id for e in self.data.truth.events})
        others = sorted(set(self.data.truth.observation_vehicles) - set(faulty))
        moved = dict(zip(faulty, others + faulty))
        shifted = [Event(moved[e.vehicle_id], e.kind, e.category, e.onset_us, e.end_us, e.params,
                         e.direction, e.horizon_us) for e in self.data.truth.events]
        wrong = metrics(score(self.oracle(shifted), self.data.truth))
        self.assertLess(wrong["excess_recall"], 0.5)
        self.assertLess(wrong["precision"], 0.5)


if __name__ == "__main__":
    unittest.main()
