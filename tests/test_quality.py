"""Time and data-quality semantics."""

import unittest

from helpers import FOCUSED, alternating, records, row, steady

from pexislink.config import DetectorConfig
from pexislink.ingest import InputError
from pexislink.quality import QualityTracker


def annotate(rows, config=FOCUSED, **kwargs):
    tracker = QualityTracker(config, **kwargs)
    return [tracker.annotate(r) for r in records(rows)]


class IntegrityTests(unittest.TestCase):
    def test_states(self):
        items = annotate([
            row(0, 90), row(30, 91), row(30, 91),      # exact duplicate
            row(30, 95),                                # conflicting duplicate
            row(10, 90),                                # out of order
            ("garbage", "v1", "1", "1", "90", "30"),    # unplaceable
            row(60, 90),
        ])
        self.assertEqual([i.integrity for i in items], [
            "accepted", "accepted", "duplicate", "conflicting_duplicate", "out_of_order",
            "unplaceable", "accepted"])

    def test_vehicles_are_independent(self):
        items = annotate([row(100, 90, vehicle="a"), row(50, 90, vehicle="b"), row(130, 90, vehicle="a")])
        self.assertEqual([i.integrity for i in items], ["accepted"] * 3)

    def test_excluded_records_do_not_advance_the_clock(self):
        items = annotate([row(0, 90), row(-30, 90), row(30, 90)])
        self.assertEqual(items[2].segment_elapsed_s, 30.0)

    def test_vehicle_limit(self):
        with self.assertRaisesRegex(InputError, "more than 2 vehicles"):
            annotate([row(0, 90, vehicle=f"v{i}") for i in range(3)], max_vehicles=2)


class SegmentAndWarmupTests(unittest.TestCase):
    def test_segment_boundary_is_strictly_greater_than_gap(self):
        cfg = DetectorConfig(segment_gap_s=900, warmup_s=0)
        items = annotate([row(0, 90), row(900, 91), row(1801, 90)], cfg)
        self.assertEqual([i.segment_start for i in items], [True, False, True])

    def test_warmup_window(self):
        cfg = DetectorConfig(warmup_s=900)
        items = annotate([row(0, 40), row(899, 80), row(900, 90), row(901, 91)], cfg)
        self.assertEqual([i.warmup for i in items], [True, True, False, False])
        self.assertIn("warmup_transient", items[0].issues)

    def test_first_record_of_a_vehicle_starts_a_segment(self):
        items = annotate([row(0, 90, vehicle="a"), row(10, 90, vehicle="b")])
        self.assertTrue(all(i.segment_start for i in items))


class PlausibilityTests(unittest.TestCase):
    def test_range_edges_are_inclusive(self):
        items = annotate([row(0, -39), row(30, 214), row(60, 90, rpm=0, speed=0, load=0),
                          row(90, 91, rpm=10000, speed=300, load=100)])
        self.assertTrue(all(i.coolant_ok and i.context_ok and i.load_ok for i in items))

    def test_obd_encoding_extremes_are_sentinels(self):
        """-40 C and 215 C are what OBD-II PID 0x05 reports for a broken sensor."""
        items = annotate([row(0, -40), row(30, 215)])
        self.assertTrue(all("coolant_temp_c_sentinel" in i.issues and not i.coolant_plausible for i in items))

    def test_out_of_range_values_are_flagged_not_trusted(self):
        items = annotate([row(0, 216), row(30, -41), row(60, 90, rpm=10001),
                          row(90, 90, speed=-1), row(120, 91, load=101)])
        self.assertEqual([i.coolant_ok for i in items], [False, False, True, True, True])
        self.assertIn("coolant_temp_c_implausible", items[0].issues)
        self.assertFalse(items[2].context_ok)
        self.assertFalse(items[3].context_ok)
        self.assertFalse(items[4].load_ok)
        self.assertIn("engine_load_pct_implausible", items[4].issues)

    def test_readings_above_the_old_150_c_ceiling_are_plausible(self):
        """Code review L1: a real catastrophic overheat must stay assessable."""
        (item,) = annotate([row(0, 160)])
        self.assertTrue(item.coolant_plausible)


class StuckSensorTests(unittest.TestCase):
    def flags(self, rows, config=FOCUSED):
        return ["coolant_stuck_suspected" in i.issues for i in annotate(rows, config)]

    def test_identical_across_engine_off_gap(self):
        # 10 minutes driving ending at 90 C, 2 h parked, restart still reading 90 C.
        rows = alternating(0, 20) + [row(600, 90)] + steady(600 + 7200, 6, 90)
        self.assertEqual(self.flags(rows)[-6:], [False, False, False, False, True, True])

    def test_dithering_frozen_sensor_is_suspected(self):
        """Code review M2: a frozen sensor flickering by 1 LSB evaded bit-equality rules."""
        rows = alternating(0, 20) + alternating(7200, 30)  # 90/91 before and after a 2 h park
        self.assertEqual(self.flags(rows)[20:26], [False, False, False, False, True, True])
        self.assertTrue(all(self.flags(rows)[24:]))

    def test_single_outlier_does_not_clear_suspicion_but_two_do(self):
        rows = [row(0, 90)] + steady(7200, 6, 90) + [row(7380, 120), row(7410, 90), row(7440, 90),
                                                     row(7470, 70), row(7500, 72), row(7530, 90)]
        flags = self.flags(rows)
        self.assertTrue(flags[6])                    # suspected
        self.assertEqual(flags[7:10], [False, True, True])   # a lone outlier, then still suspected
        self.assertEqual(flags[10:], [False, False, False])  # two deviations clear it

    def test_sentinel_spikes_do_not_reset_the_stuck_state(self):
        """Scientific review F5: a -40 sentinel used to reset the rule on a frozen sensor."""
        rows = [row(0, 90)] + steady(7200, 6, 90) + [row(7380, -40)] + steady(7410, 3, 90)
        self.assertTrue(all(self.flags(rows)[-3:]))

    def test_startup_cached_value_is_not_suspicious(self):
        # Some loggers repeat the last value once at start-up, then report real data.
        rows = [row(0, 90), row(7200, 90)] + [row(7230 + 30 * i, 40 + 5 * i) for i in range(10)]
        self.assertFalse(any(self.flags(rows)))

    def test_short_gap_does_not_trigger_gap_rule(self):
        rows = [row(0, 90)] + steady(1800, 10, 90)  # 30 min gap < stuck_gap_s
        self.assertFalse(any(self.flags(rows)))

    def test_long_steady_cruise_is_never_suspected(self):
        """Code review M5: the removed flatline rule flagged 134 of 225 rows of a
        healthy 105 min cruise at a steady, thermostat-regulated 90 C."""
        self.assertFalse(any(self.flags(steady(0, 225, 90), DetectorConfig(warmup_s=0))))

    def test_realistic_noise_is_never_flagged(self):
        self.assertFalse(any(self.flags(alternating(0, 400), DetectorConfig(warmup_s=0))))


if __name__ == "__main__":
    unittest.main()
