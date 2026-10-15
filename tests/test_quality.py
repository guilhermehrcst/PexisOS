"""Time and data-quality semantics."""

import random
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
        # suspect from the first post-park reading (provisional until the 5th establishes it)
        self.assertEqual(self.flags(rows)[-6:], [True] * 6)

    def test_dithering_frozen_sensor_is_suspected(self):
        """Code review M2: a frozen sensor flickering by 1 LSB evaded bit-equality rules."""
        rows = alternating(0, 20) + alternating(7200, 30)  # 90/91 before and after a 2 h park
        self.assertTrue(all(self.flags(rows)[20:]))

    def test_single_outlier_does_not_clear_suspicion_but_two_do(self):
        rows = [row(0, 90)] + steady(7200, 6, 90) + [row(7380, 120), row(7410, 90), row(7440, 90),
                                                     row(7470, 70), row(7500, 72), row(7530, 90)]
        flags = self.flags(rows)
        self.assertTrue(flags[6])                           # suspected
        # The lone outlier is read while the suspicion is unresolved, so it stays
        # suspected. (Until the sensor-integrity review it was emitted unflagged
        # although the state was still "stuck": a reading the system did not trust
        # could be reported normal.)
        self.assertEqual(flags[7:10], [True, True, True])
        self.assertEqual(flags[10:], [True, False, False])  # the 2nd consecutive deviation clears it

    def test_sentinel_spikes_do_not_reset_the_stuck_state(self):
        """Scientific review F5: a -40 sentinel used to reset the rule on a frozen sensor."""
        rows = [row(0, 90)] + steady(7200, 6, 90) + [row(7380, -40)] + steady(7410, 3, 90)
        self.assertTrue(all(self.flags(rows)[-3:]))

    def test_startup_cached_value_is_not_suspicious(self):
        # Some loggers repeat the last value once at start-up, then report real data.
        # The cached value itself is withheld (one provisional suspicion); the first
        # real reading moves away from it and is trusted again at once.
        rows = [row(0, 90), row(7200, 90)] + [row(7230 + 30 * i, 40 + 5 * i) for i in range(10)]
        self.assertEqual(self.flags(rows), [False, True] + [False] * 10)

    def test_short_gap_does_not_trigger_gap_rule(self):
        rows = [row(0, 90)] + steady(1800, 10, 90)  # 30 min gap < stuck_gap_s
        self.assertFalse(any(self.flags(rows)))

    def test_long_steady_cruise_is_never_suspected(self):
        """Code review M5: the removed flatline rule flagged 134 of 225 rows of a
        healthy 105 min cruise at a steady, thermostat-regulated 90 C."""
        self.assertFalse(any(self.flags(steady(0, 225, 90), DetectorConfig(warmup_s=0))))

    def test_realistic_noise_is_never_flagged(self):
        self.assertFalse(any(self.flags(alternating(0, 400), DetectorConfig(warmup_s=0))))


ANCHOR = [row(0, 90)] + steady(7200, 6, 90)  # 90 C, a 2 h park, then 6 readings frozen at 90: suspected


class StuckStateMachineTests(unittest.TestCase):
    """Sensor-integrity review: the suspicion is a state machine, and every reading
    emitted while the suspicion is unresolved must carry it.

    States per vehicle: HEALTHY -> OBSERVING (after an engine-off gap of at least
    stuck_gap_s; anchor = the last plausible reading before the gap) -> SUSPECTED
    (stuck_min_samples readings within stuck_tolerance_c of the anchor) -> HEALTHY
    (two consecutive plausible readings outside the tolerance). Readings within
    the tolerance in OBSERVING are already suspect (provisional); a deviation
    there returns to HEALTHY at once and is trusted. In SUSPECTED a reading within
    the tolerance, an accepted reading with no usable coolant value, or a
    conflicting duplicate restarts the recovery count. Other excluded records
    (duplicate, out of order, unplaceable) never change the state."""

    def flags(self, rows, config=FOCUSED):
        return ["coolant_stuck_suspected" in i.issues for i in annotate(rows, config)]

    def test_full_transition_sequence(self):
        rows = (alternating(0, 4)                                   # 1 healthy
                + steady(7200, 5, 90)                               # 2-3 frozen after a park: suspected at the 5th
                + [row(7350, 90), row(7380, 93), row(7410, 90),     # 4 still frozen, 5 one outlier, 6 back
                   row(7440, 70), row(7470, 72),                    # 7 two consecutive deviations
                   row(7500, 74)])                                  # 8 cleared
        self.assertEqual(self.flags(rows), [False] * 4 + [True] * 5      # provisional, then established
                         + [True, True, True, True, False, False])

    def test_repeated_isolated_outliers_never_clear(self):
        outliers = [row(7380 + 60 * i, 80 + i) for i in range(5)]
        frozen = [row(7410 + 60 * i, 90) for i in range(5)]
        rows = ANCHOR + [r for pair in zip(outliers, frozen) for r in pair]
        self.assertTrue(all(self.flags(rows)[6:]))

    def test_unusable_readings_interrupt_recovery(self):
        """Invariant F: a gap in usable data must not stitch two isolated outliers
        into recovery evidence (outlier, sentinel or empty cell, outlier)."""
        for interruption in (-40, 215, None, 400):
            rows = ANCHOR + [row(7380, 80), row(7410, interruption), row(7440, 81), row(7470, 90)]
            with self.subTest(interruption=interruption):
                flags = self.flags(rows)
                self.assertEqual((flags[7], flags[9], flags[10]), (True, True, True))  # outlier, outlier, frozen

    def test_excluded_records_never_establish_or_clear(self):
        stuck = ANCHOR + [row(7380, 70), row(7380, 70),             # a deviation and its duplicate
                          row(7300, 60), row(7320, 61),             # out-of-order deviations
                          row(7380, 75),                            # conflicting duplicate
                          row(7410, 90)]                            # back at the frozen value
        items = annotate(stuck)
        self.assertEqual([i.integrity for i in items[8:12]],
                         ["duplicate", "out_of_order", "out_of_order", "conflicting_duplicate"])
        # excluded deviations did not complete the recovery evidence
        self.assertTrue(items[-1].coolant_plausible and "coolant_stuck_suspected" in items[-1].issues)
        # never established by excluded rows either: duplicates of the frozen value after a park
        healthy = [row(0, 90), row(7200, 90)] + [row(7200, 90)] * 6 + [row(7230, 60)]
        self.assertEqual(self.flags(healthy), [False, True] + [False] * 7)  # one provisional, never established

    def test_conflicting_duplicate_interrupts_recovery(self):
        """Adversarial review L1: a deviation contradicted by a conflicting duplicate
        is not evidence; the detector already drops its run for the same reason."""
        rows = ANCHOR + [row(7380, 70), row(7380, 90), row(7410, 72), row(7440, 90)]
        items = annotate(rows)
        self.assertEqual(items[8].integrity, "conflicting_duplicate")
        self.assertEqual(["coolant_stuck_suspected" in items[i].issues for i in (7, 9, 10)], [True, True, True])

    def test_suspicion_survives_another_park_and_one_quantisation_step(self):
        rows = ANCHOR + alternating(7380, 6) + steady(7380 + 180 + 7200, 4, 91)
        self.assertTrue(all(self.flags(rows)[6:]))

    def test_recovery_across_a_park_is_two_real_readings(self):
        # a working sensor reads the cooled engine after a park: that is genuine evidence
        rows = ANCHOR + [row(7380, 70), row(7380 + 7200, 40), row(7380 + 7230, 45)]
        self.assertEqual(self.flags(rows)[6:], [True, True, False, False])

    def test_operating_conditions_do_not_matter(self):
        rows = ANCHOR + [row(7380 + 30 * i, 90, rpm=800 + 400 * i, speed=10 * i) for i in range(8)]
        self.assertTrue(all(self.flags(rows)[6:]))

    def test_vehicles_are_isolated(self):
        frozen = ANCHOR + [row(7380, 93), row(7410, 90)]
        other = [row(t, 60 + (t // 30) % 7, vehicle="v2") for t in range(0, 7440, 30)]
        merged = sorted(frozen + other, key=lambda r: (r[0], r[1]))
        by_vehicle = {}
        for r, i in zip(merged, annotate(merged)):
            by_vehicle.setdefault(r[1], []).append("coolant_stuck_suspected" in i.issues)
        self.assertEqual(by_vehicle["v1"], self.flags(frozen))
        self.assertEqual(by_vehicle["v2"], self.flags(other))


def oracle_flags(rows, config):
    """Independent, explicit statement of the state machine documented above."""
    tol, need, gap_us = config.stuck_tolerance_c, config.stuck_min_samples, round(config.stuck_gap_s * 1e6)
    lo, hi = config.coolant_range_c
    state = {}
    out = []
    for record in records(rows):
        vid, now, value = record.vehicle_id, record.epoch_us, record.coolant_temp_c
        s = state.setdefault(vid, {"mode": "HEALTHY", "anchor": None, "k": 0, "d": 0,
                                   "prev": None, "prev_us": None, "last_us": None, "content": None})
        content = (record.engine_rpm, record.speed_kmh, value, record.engine_load_pct, record.issues)
        if s["last_us"] is not None and now <= s["last_us"]:
            if now == s["last_us"] and content != s["content"] and s["mode"] == "SUSPECTED":
                s["d"] = 0                       # conflicting duplicate restarts recovery
            out.append(False)                    # excluded record
            continue
        s["last_us"], s["content"] = now, content
        usable = value is not None and lo <= value <= hi and value not in config.coolant_sentinels_c
        if not usable:
            if s["mode"] == "SUSPECTED":
                s["d"] = 0
            out.append(False)
            continue
        if s["prev_us"] is not None and now - s["prev_us"] >= gap_us and s["mode"] != "SUSPECTED":
            s["mode"], s["anchor"], s["k"], s["d"] = "OBSERVING", s["prev"], 0, 0
        s["prev"], s["prev_us"] = value, now
        within = s["anchor"] is not None and abs(value - s["anchor"]) <= tol
        provisional = s["mode"] == "OBSERVING" and within
        if s["mode"] == "OBSERVING":
            if within:
                s["k"] += 1
                if s["k"] >= need:
                    s["mode"], s["d"] = "SUSPECTED", 0
            else:
                s["mode"], s["anchor"] = "HEALTHY", None
        elif s["mode"] == "SUSPECTED":
            if within:
                s["d"] = 0
            else:
                s["d"] += 1
                if s["d"] >= 2:
                    s["mode"], s["anchor"], s["d"] = "HEALTHY", None, 0
        out.append(provisional or s["mode"] == "SUSPECTED")
    return out


def random_stuck_rows(rng, n=160, start_s=0):
    rows, t = [], {"a": start_s, "b": start_s}
    frozen = {"a": 90.0, "b": 88.0}
    for _ in range(n):
        vid = rng.choice("ab")
        p = rng.random()
        if p < 0.12:
            t[vid] += rng.choice([3700, 7200])           # engine-off gap
        elif p < 0.16 and rows and rows[-1][1] == vid:
            rows.append(rows[-1])                        # exact duplicate
            continue
        elif p < 0.19:
            rows.append(row(max(t[vid] - 45, start_s), 70, vehicle=vid))
            continue                                     # late row
        elif p < 0.22 and rows and rows[-1][1] == vid:
            rows.append(rows[-1][:4] + (str(rng.choice([60, 75, 90, 120])),) + rows[-1][5:])
            continue                                     # same instant, other content: usually conflicting
        t[vid] += 30
        q = rng.random()
        value = (frozen[vid] if q < 0.6 else frozen[vid] + rng.choice([-1, 1]) if q < 0.7
                 else rng.choice([None, -40, 215, 500]) if q < 0.78 else frozen[vid] + rng.uniform(-30, 30))
        rows.append(row(t[vid], None if value is None else round(value, 1), vehicle=vid))
    return rows


class StuckModelBasedTests(unittest.TestCase):
    def test_tracker_matches_the_specified_state_machine(self):
        for seed in range(300):
            rows = random_stuck_rows(random.Random(seed))
            with self.subTest(seed=seed):
                got = ["coolant_stuck_suspected" in i.issues for i in annotate(rows)]
                self.assertEqual(got, oracle_flags(rows, FOCUSED))


if __name__ == "__main__":
    unittest.main()
