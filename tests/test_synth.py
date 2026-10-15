"""Synthetic generator: determinism, label separation, causality, physics sanity."""

import statistics
import unittest
from collections import defaultdict
from functools import lru_cache

from pexislink.config import DetectorConfig
from pexislink.ingest import CANONICAL_COLUMNS, parse_rows, parse_timestamp
from pexislink.quality import QualityTracker
from pexislink.synth import DAY_US, SCENARIOS, generate

SMALL = dict(reference_days=6, eval_days=14)


@lru_cache(maxsize=None)
def data(scenario, seed=11):
    return generate(scenario, seed, **SMALL)


def rows_of(rows, vehicle):
    return [r for r in rows if r[1] == vehicle]


def epoch(row):
    return parse_timestamp(row[0])[1]


class DeterminismAndSchemaTests(unittest.TestCase):
    def test_same_seed_same_bytes_different_seed_different_data(self):
        self.assertEqual(generate("abrupt_anomaly", 5, **SMALL), generate("abrupt_anomaly", 5, **SMALL))
        self.assertNotEqual(generate("abrupt_anomaly", 5, **SMALL).observation_rows,
                            generate("abrupt_anomaly", 6, **SMALL).observation_rows)

    def test_rows_are_schema_only_and_parse_cleanly(self):
        for name in ("normal_operation", "abrupt_anomaly", "load_change"):
            dataset = data(name)
            for rows in (dataset.reference_rows, dataset.observation_rows):
                self.assertTrue(all(len(r) == len(CANONICAL_COLUMNS) for r in rows))
                issues = {i for record in parse_rows(rows) for i in record.issues}
                self.assertEqual(issues, set(), name)

    def test_truth_is_aligned_with_rows_and_kept_separate(self):
        dataset = data("data_integrity")
        self.assertEqual(len(dataset.truth.observation_epochs_us), len(dataset.observation_rows))
        for r, vehicle, true_us in zip(dataset.observation_rows, dataset.truth.observation_vehicles,
                                       dataset.truth.observation_epochs_us):
            self.assertEqual((r[1], epoch(r)), (vehicle, true_us))

    def test_option_floor(self):
        with self.assertRaises(ValueError):
            generate("normal_operation", 1, eval_days=5)


class CausalityTests(unittest.TestCase):
    """No future information in the reference period: injections use a separate
    random stream, so reference rows equal the no-injection control byte for byte."""

    def test_reference_rows_identical_to_control_for_eval_only_injections(self):
        control = data("normal_operation").reference_rows
        for name in ("abrupt_anomaly", "gradual_sensor_drift", "load_change", "seasonal_shift",
                     "unreliable_sensor", "data_integrity"):
            self.assertEqual(data(name).reference_rows, control, name)

    def test_observations_identical_to_control_until_onset(self):
        control = data("normal_operation").observation_rows
        dataset = data("abrupt_anomaly")
        for event in dataset.truth.events:
            before = [r for r in rows_of(dataset.observation_rows, event.vehicle_id) if epoch(r) < event.onset_us]
            control_before = [r for r in rows_of(control, event.vehicle_id) if epoch(r) < event.onset_us]
            self.assertGreater(len(before), 100)
            self.assertEqual(before, control_before, event.kind)
            after = [r for r in rows_of(dataset.observation_rows, event.vehicle_id) if epoch(r) >= event.onset_us]
            control_after = [r for r in rows_of(control, event.vehicle_id) if epoch(r) >= event.onset_us]
            self.assertNotEqual(after, control_after)

    def test_contaminated_reference_differs_only_for_contaminated_vehicles(self):
        control, dataset = data("normal_operation"), data("contaminated_reference")
        for k in range(8):
            vehicle = f"veh-{k:02d}"
            same = rows_of(dataset.reference_rows, vehicle) == rows_of(control.reference_rows, vehicle)
            self.assertEqual(same, k > 3, vehicle)


class PhysicsSanityTests(unittest.TestCase):
    def warm_values(self, rows):
        """Coolant readings more than 25 min into a segment, keyed by speed regime."""
        out, start, prev = defaultdict(list), {}, {}
        for r in rows:
            t, vehicle = epoch(r), r[1]
            if vehicle not in prev or t - prev[vehicle] > 900e6:
                start[vehicle] = t
            prev[vehicle] = t
            if t - start[vehicle] > 1500e6 and r[4]:
                speed = float(r[3])
                out["idle" if speed < 3 else "highway" if speed > 75 else "urban"].append(int(r[4]))
        return out

    def test_regulated_level_noise_ordering_and_quantisation(self):
        warm = self.warm_values(data("normal_operation").reference_rows)
        median = statistics.median(warm["urban"])
        self.assertTrue(83 <= median <= 98, median)
        self.assertGreater(statistics.pstdev(warm["idle"]), statistics.pstdev(warm["highway"]))
        self.assertTrue(all(r[4].lstrip("-").isdigit() for r in data("normal_operation").observation_rows))

    def test_every_trip_starts_cold_and_warms_up(self):
        rows = rows_of(data("normal_operation").reference_rows, "veh-03")
        firsts, prev = [], None
        for r in rows:
            t = epoch(r)
            if prev is None or t - prev > 900e6:
                firsts.append(int(r[4]))
            prev = t
        self.assertLess(statistics.median(firsts), 60)

    def test_injected_faults_have_the_documented_physical_signature(self):
        control = {r[0] + r[1]: r for r in data("normal_operation").observation_rows}
        for name in ("abrupt_anomaly", "gradual_sensor_drift"):
            dataset = data(name)
            for event in dataset.truth.events:
                shifts = [int(r[4]) - int(control[r[0] + r[1]][4])
                          for r in rows_of(dataset.observation_rows, event.vehicle_id)
                          if epoch(r) >= event.onset_us + 2 * DAY_US and float(r[3]) < 3
                          and r[0] + r[1] in control]
                mean_shift = statistics.mean(shifts)
                if event.kind == "overheat":
                    expected = 1
                elif event.kind == "thermostat_stuck_open":
                    expected = -1
                else:
                    expected = 1 if event.param("rate_c_per_day") > 0 else -1
                self.assertGreater(mean_shift * expected, 3, (name, event.kind, mean_shift))

    def test_stuck_sensor_is_constant_after_onset(self):
        dataset = data("unreliable_sensor")
        for event in (e for e in dataset.truth.events if e.kind == "sensor_stuck"):
            values = {r[4] for r in rows_of(dataset.observation_rows, event.vehicle_id)
                      if epoch(r) >= event.onset_us and r[4] not in ("", "-40", "215")}
            self.assertLessEqual(len(values), 1 + 40)  # stuck value plus rare +15..35 C spikes
            stuck = max(values, key=lambda v: sum(1 for r in dataset.observation_rows if r[4] == v
                                                   and r[1] == event.vehicle_id))
            self.assertTrue(stuck.lstrip("-").isdigit())

    def test_unseen_vehicles_have_no_reference_and_unseen_regime_has_no_highway(self):
        dataset = data("unseen_vehicles_and_regimes")
        vehicles = {r[1] for r in dataset.reference_rows}
        self.assertTrue({"veh-00", "veh-01", "veh-02"}.isdisjoint(vehicles))
        self.assertEqual(dataset.truth.vehicles_without_reference, {"veh-00", "veh-01", "veh-02"})
        for vehicle in ("veh-03", "veh-04", "veh-05"):
            self.assertLess(max(float(r[3]) for r in rows_of(dataset.reference_rows, vehicle)), 80)
            self.assertGreater(max(float(r[3]) for r in rows_of(dataset.observation_rows, vehicle)), 80)

    def test_integrity_scenario_exercises_every_integrity_state(self):
        tracker = QualityTracker(DetectorConfig())
        states = {tracker.annotate(r).integrity for r in parse_rows(data("data_integrity").observation_rows)}
        self.assertEqual(states, {"accepted", "duplicate", "conflicting_duplicate", "out_of_order"})
        suffixes = {r[0][19:] for r in data("data_integrity").observation_rows}
        self.assertEqual(suffixes, {"Z", "+02:00", "-03:00", "+05:30"})

    def test_cold_season_warmup_exceeds_the_detector_window(self):
        """Deliberate assumption violation for hypothesis H6: in severe cold some
        trips are still >= 5 C below the warm level 15 min after the start."""
        slow = 0
        for k in range(4, 8):  # cold-season vehicles
            segments, current, prev = [], [], None
            for r in rows_of(data("seasonal_shift").observation_rows, f"veh-{k:02d}"):
                t = epoch(r)
                if prev is not None and t - prev > 900e6:
                    segments.append(current)
                    current = []
                current.append((t, int(r[4])))
                prev = t
            segments.append(current)
            level = statistics.median(c for s in segments for t, c in s if t - s[0][0] > 1500e6)
            slow += sum(1 for s in segments for t, c in s
                        if 900e6 <= t - s[0][0] < 960e6 and c <= level - 5)
        self.assertGreaterEqual(slow, 5)

    def test_every_scenario_generates(self):
        for name in SCENARIOS:
            dataset = generate(name, 3, **SMALL)
            self.assertGreater(len(dataset.observation_rows), 1000, name)


if __name__ == "__main__":
    unittest.main()
