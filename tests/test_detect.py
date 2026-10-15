"""Detector behaviour: v0.2 counterparts of every audit finding, an independent
oracle for the confirmation rule, and metamorphic invariants."""

import json
import random
import unittest
from dataclasses import replace

from helpers import FOCUSED, alternating, records, row, steady

from pexislink.config import DetectorConfig
from pexislink.detect import STATUSES, assess_records
from pexislink.reference import build_reference

DAY = 86_400


def run(reference_rows, observed_rows, config=FOCUSED, use_load=False):
    model = build_reference(records(reference_rows), config, use_load=use_load)
    return list(assess_records(model, records(observed_rows)))


def statuses(assessments):
    return [a.status for a in assessments]


REFERENCE = alternating(0, 40)  # v1, rpm 2000, speed 60, load 30, 90/91 C


class WarmupTests(unittest.TestCase):
    """F-02 counterpart: an ordinary cold start is a transient, not an anomaly."""

    def test_cold_start_with_default_configuration(self):
        cfg = DetectorConfig()
        reference = alternating(0, 80)  # 40 min: first 15 min excluded as warm-up
        ramp = [row(DAY + 60 * m, t) for m, t in enumerate((25, 35, 45, 55, 65, 75, 82, 88))]
        warm = alternating(DAY + 480, 50)
        result = run(reference, ramp + warm, cfg)
        self.assertNotIn("suspected_anomaly", statuses(result))
        self.assertNotIn("confirmed_anomaly", statuses(result))
        self.assertTrue(all(a.reasons == ("warmup_transient",) for a in result[:8]))
        self.assertEqual(statuses(result)[-1], "normal")
        first_normal = statuses(result).index("normal")
        self.assertGreaterEqual(result[first_normal].epoch_us - result[0].epoch_us, 900 * 10**6)


class LoadContextTests(unittest.TestCase):
    """F-02 counterpart: a legitimate load increase is not identifiable without a
    load signal; with it, v0.2 abstains instead of raising a false alarm."""

    def setUp(self):
        self.cfg = replace(FOCUSED, min_reference_samples=30)
        self.reference = alternating(0, 60, load=30) + alternating(1800, 10, low=99, high=100, load=80)
        self.towing = steady(DAY, 20, 99.5, load=80)

    def test_with_load_signal_the_unfamiliar_load_is_unassessable(self):
        result = run(self.reference, self.towing, self.cfg, use_load=True)
        self.assertEqual(set(a.reasons for a in result), {("no_reference_context",)})

    def test_without_load_signal_it_is_a_false_alarm(self):
        result = run(self.reference, self.towing, self.cfg, use_load=False)
        self.assertEqual(statuses(result)[-1], "confirmed_anomaly")


class PersistenceTests(unittest.TestCase):
    """F-03 and F-05 counterparts: confirmation is a continuous duration."""

    def test_run_does_not_bridge_gaps(self):
        # 106, not 103 again: an identical reading after 28 days is a frozen-sensor
        # signature and is (correctly) withheld, which would hide what is tested here
        result = run(REFERENCE, [row(DAY, 103), row(29 * DAY, 106)])
        self.assertEqual(statuses(result), ["suspected_anomaly", "suspected_anomaly"])

    def test_confirmation_requires_duration(self):
        result = run(REFERENCE, [row(DAY, 104), row(DAY + 0.1, 104)])
        self.assertEqual(statuses(result), ["suspected_anomaly"] * 2)
        result = run(REFERENCE, steady(DAY, 11, 104))  # 0 .. 300 s
        self.assertEqual(statuses(result), ["suspected_anomaly"] * 10 + ["confirmed_anomaly"])
        self.assertEqual(result[-1].run_s, 300.0)

    def test_context_change_keeps_run(self):
        reference = alternating(0, 40, rpm=1990) + alternating(2000, 40, rpm=2010)
        jitter = [row(DAY + 30 * i, 104, rpm=1990 if i % 2 == 0 else 2010) for i in range(12)]
        self.assertEqual(statuses(run(reference, jitter))[-1], "confirmed_anomaly")

    def test_unassessable_sample_is_neutral(self):
        rows = steady(DAY, 5, 104) + [row(DAY + 150, 104, rpm=5000)] + steady(DAY + 180, 5, 104)
        result = run(REFERENCE, rows)
        self.assertEqual(result[5].reasons, ("no_reference_context",))
        self.assertEqual(statuses(result)[-1], "confirmed_anomaly")

    def test_evidence_gap_beyond_run_gap_resets(self):
        rows = steady(DAY, 6, 104) + steady(DAY + 150 + 91, 6, 104)
        result = run(REFERENCE, rows)
        self.assertNotIn("confirmed_anomaly", statuses(result))

    def test_direction_flip_and_normal_reset(self):
        for interruption in (80, 90):  # opposite direction, then a normal reading
            rows = steady(DAY, 6, 104) + [row(DAY + 180, interruption)] + steady(DAY + 210, 6, 104)
            self.assertNotIn("confirmed_anomaly", statuses(run(REFERENCE, rows)))


class ThresholdBoundaryTests(unittest.TestCase):
    """|delta| >= threshold is a deviation (inclusive, as documented). Added after
    a mutation check showed '>=' -> '>' survived the suite."""

    def test_boundary_is_inclusive_on_both_sides(self):
        reference = steady(0, 40, lambda i: (90, 90, 89, 91)[i % 4])  # median 90, scale -> 1 C
        result = run(reference, [row(DAY, 94), row(DAY + 30, 93.9), row(DAY + 60, 86), row(DAY + 90, 86.1)])
        self.assertEqual([a.threshold_c for a in result], [4.0] * 4)
        self.assertEqual([a.reasons for a in result],
                         [("above_reference",), (), ("below_reference",), ()])


class StuckSensorTests(unittest.TestCase):
    """F-01 counterpart: a frozen sensor stops producing 'normal'."""

    def test_frozen_sensor_across_a_park(self):
        trip1 = alternating(DAY, 40) + steady(DAY + 1200, 40, 90)   # sensor freezes at 90
        trip2 = steady(DAY + 1200 + 1170 + 7200, 60, 90)             # 2 h later, still 90
        result = run(REFERENCE, trip1 + trip2)
        tail = result[len(trip1) + 4:]
        self.assertTrue(all(a.reasons == ("coolant_stuck_suspected",) for a in tail))
        self.assertNotIn("normal", statuses(tail))

    def test_stuck_high_sensor_still_alarms(self):
        """Code review M5: stuck suspicion used to veto the absolute barrier, so a
        118 C plateau was silenced. A sensor stuck high must fail towards an alarm."""
        hot = [row(DAY, 117)] + steady(DAY + 7200, 30, 118)
        result = run(REFERENCE, hot)
        self.assertIn("coolant_stuck_suspected", result[-1].quality)
        self.assertEqual((result[-1].status, result[-1].reasons), ("confirmed_anomaly", ("above_absolute_limit",)))


class SensorIntegrityStateTests(unittest.TestCase):
    """Sensor-integrity review. Invariant A: while a frozen-sensor suspicion is
    unresolved, no reading may be reported normal, nor assessed by the relative
    barrier. Invariant D: the absolute barrier still evaluates every plausible
    reading, so a hot reading stays an (unconfirmed, qualified) alarm."""

    FROZEN = alternating(DAY, 20) + [row(DAY + 600, 90)] + steady(DAY + 600 + 7200, 6, 90)  # suspected at the end

    def test_single_plausible_outlier_is_not_normal(self):
        # 93 C is 2.5 C from the 90.5 C reference median, inside the 4 C threshold:
        # before the fix this reading came out "normal" while the state was still stuck.
        result = run(REFERENCE, self.FROZEN + [row(DAY + 8010, 93), row(DAY + 8040, 90)])
        self.assertEqual([(a.status, a.reasons) for a in result[-2:]],
                         [("insufficient_evidence", ("coolant_stuck_suspected",))] * 2)

    def test_relative_barrier_waits_for_explicit_recovery(self):
        tail = [row(DAY + 8010, 94.5), row(DAY + 8040, 95), row(DAY + 8070, 92)]
        result = run(REFERENCE, self.FROZEN + tail)[-3:]
        self.assertEqual(result[0].reasons, ("coolant_stuck_suspected",))   # 1st deviation: still suspected
        self.assertEqual(result[1].reasons, ("above_reference",))           # 2nd consecutive: recovered
        self.assertEqual(result[2].status, "normal")

    def test_absolute_barrier_stays_observable_during_suspicion(self):
        result = run(REFERENCE, self.FROZEN + [row(DAY + 8010, 120), row(DAY + 8040, 90)])
        hot = result[-2]
        self.assertEqual((hot.status, hot.reasons), ("suspected_anomaly", ("above_absolute_limit",)))
        self.assertIn("coolant_stuck_suspected", hot.quality)
        self.assertIsNone(hot.reference_c)                                   # no relative evidence used
        self.assertEqual(result[-1].reasons, ("coolant_stuck_suspected",))

    def test_post_park_readings_at_the_frozen_value_are_never_normal(self):
        """Adversarial review M1: readings after an engine-off gap that stay at the
        pre-gap value are an unresolved integrity question from the first one on.
        Before the fix they were trusted until stuck_min_samples was reached, which
        the warm-up window hid only at fast sampling and only if no unusable
        readings preceded them."""
        cfg = DetectorConfig()
        reference = alternating(0, 160)                       # 80 min, warm contexts well populated
        drive = alternating(DAY, 60)                          # warm drive ends at DAY + 1770 s
        park = DAY + 1770 + 7200
        cases = {
            "300 s sampling": [row(park + 300 * i, 90) for i in range(6)],
            "sentinels first": [row(park + 30 * i, -40) for i in range(31)] + steady(park + 930, 6, 90),
            "coolant dropout": ([row(DAY + 1800 + 30 * i, None) for i in range(130)]   # >= 3600 s, no new segment
                                + steady(DAY + 1800 + 3900, 6, 90)),
        }
        for name, tail in cases.items():
            with self.subTest(name):
                result = run(reference, drive + tail, cfg)[len(drive):]
                self.assertNotIn("normal", statuses(result))

    def test_suspicion_carries_from_the_reference_file_into_the_observations(self):
        """Adversarial review M2: each file used a fresh tracker, so a sensor frozen
        at the end of the reference period was trusted again in the observations."""
        for cfg in (FOCUSED, DetectorConfig()):
            reference = alternating(0, 120) + steady(3570 + 7200, 60, 90)   # frozen after a park
            observed = steady(3570 + 7200 + 1800, 60, 90)                    # still frozen, same stream
            with self.subTest(warmup_s=cfg.warmup_s):
                self.assertNotIn("normal", statuses(run(reference, observed, cfg)))

    def test_no_normal_while_any_vehicle_is_suspected_in_random_streams(self):
        from test_quality import oracle_flags, random_stuck_rows
        reference = alternating(0, 40) + alternating(0, 40, vehicle="a") + alternating(0, 40, vehicle="b",
                                                                                       low=88, high=89)
        for seed in range(200):
            rows = random_stuck_rows(random.Random(seed), start_s=DAY)
            result = run(reference, rows)
            with self.subTest(seed=seed):
                for assessment, suspected in zip(result, oracle_flags(rows, FOCUSED)):
                    if suspected:
                        self.assertNotEqual(assessment.status, "normal")
                        self.assertNotIn("above_reference", assessment.reasons)
                        self.assertNotIn("below_reference", assessment.reasons)


class SensorValidityTests(unittest.TestCase):
    """F-04 counterpart: sentinels are sensor problems, not engine anomalies."""

    def test_sentinels_are_insufficient_evidence(self):
        for sentinel in (215, -40):
            result = run(REFERENCE, steady(DAY, 20, sentinel))
            self.assertEqual(set(a.reasons for a in result), {("coolant_temp_c_sentinel",)})
        for impossible in (216, -41):
            result = run(REFERENCE, steady(DAY, 20, impossible))
            self.assertEqual(set(a.reasons for a in result), {("coolant_temp_c_implausible",)})


class AbsoluteBarrierTests(unittest.TestCase):
    """F-07 mitigation: a reference-free barrier with an independent premise."""

    def test_contaminated_reference_is_caught_by_absolute_limit(self):
        hot_reference = alternating(0, 40, low=113, high=114)
        result = run(hot_reference, steady(DAY, 11, 116))
        self.assertEqual(result[-1].status, "confirmed_anomaly")
        self.assertEqual(result[-1].reasons, ("above_absolute_limit",))
        self.assertEqual(statuses(run(hot_reference, steady(DAY, 11, 113))), ["normal"] * 11)

    def test_unseen_vehicle_alarm_but_never_normal(self):
        hot = run(REFERENCE, steady(DAY, 11, 120, vehicle="new"))
        self.assertEqual(hot[-1].status, "confirmed_anomaly")
        cool = run(REFERENCE, steady(DAY, 11, 90, vehicle="new"))
        self.assertEqual(set(a.reasons for a in cool), {("no_reference_vehicle",)})

    def test_readings_above_150_c_are_alarms_not_implausible(self):
        """Code review L1: the barrier used to stop working above 150 C."""
        result = run(REFERENCE, steady(DAY, 11, 160))
        self.assertEqual(result[-1].status, "confirmed_anomaly")
        self.assertIn("above_absolute_limit", result[-1].reasons)

    def test_absolute_barrier_works_during_warmup(self):
        result = run(alternating(0, 80), steady(DAY, 11, 118), DetectorConfig())
        self.assertEqual(result[-1].status, "confirmed_anomaly")

    def test_barrier_can_be_disabled(self):
        result = run(REFERENCE, steady(DAY, 11, 120, vehicle="new"), replace(FOCUSED, absolute_high_c=None))
        self.assertEqual(set(statuses(result)), {"insufficient_evidence"})


class IntegrityTests(unittest.TestCase):
    """F-08 counterpart: integrity problems are quarantined per record."""

    def test_duplicates_and_late_rows_do_not_abort_or_leak(self):
        rows = [row(DAY, 90, vehicle="other"), row(DAY + 30, 90), row(DAY + 30, 90),
                row(DAY + 60, 91), row(DAY + 10, 90), row(DAY + 90, 90)]
        result = run(REFERENCE + alternating(0, 40, vehicle="other"), rows)
        self.assertEqual([a.reasons[:1] if a.reasons else () for a in result],
                         [(), (), ("duplicate",), (), ("out_of_order",), ()])

    def test_conflicting_duplicate_resets_the_evidence_run(self):
        rows = steady(DAY, 9, 104) + [row(DAY + 240, 70)] + steady(DAY + 270, 2, 104)
        result = run(REFERENCE, rows)
        self.assertEqual(result[9].reasons, ("conflicting_duplicate",))
        self.assertNotIn("confirmed_anomaly", statuses(result))

    def test_reference_overlap_is_quarantined_per_record(self):
        """Code review M3: one overlapping row used to abort the fleet."""
        result = run(REFERENCE + alternating(0, 40, vehicle="other"),
                     [row(600, 90), row(DAY, 90, vehicle="other")])
        self.assertEqual(result[0].reasons, ("reference_overlap",))
        self.assertEqual(result[1].status, "normal")


class OutputContractTests(unittest.TestCase):
    def test_one_assessment_per_input_row_in_order(self):
        rows = [("bad", "", "", "", "", "")] + steady(DAY, 5, 90) + [("x",)]
        result = run(REFERENCE, rows)
        self.assertEqual([a.line for a in result], list(range(2, 2 + len(rows))))
        self.assertEqual(result[0].reasons, ("unplaceable",))
        self.assertEqual(result[-1].reasons, ("unplaceable",))
        self.assertIn("row_field_count", result[-1].quality)

    def test_direction_property(self):
        result = run(REFERENCE, [row(DAY, 104), row(DAY + 600, 60), row(DAY + 1200, 91)])
        self.assertEqual([a.direction for a in result], [1, -1, 0])

    def test_json_contract(self):
        (assessment,) = run(REFERENCE, [row(DAY, 91)])
        payload = json.loads(json.dumps(assessment.to_json()))
        self.assertEqual(payload["schema"], "pexislink.assessment.v2")
        self.assertEqual(set(payload), {"schema", "line", "vehicle_id", "timestamp", "status", "reasons",
                                        "quality", "coolant_temp_c", "reference_c", "delta_c",
                                        "threshold_c", "reference_n", "run_s"})


# --------------------------------------------------------------------------
# Property tests with an independent oracle
# --------------------------------------------------------------------------

PROPERTY_CONFIG = replace(FOCUSED, warmup_s=240.0, segment_gap_s=900.0)
VEHICLES = ("a", "b", "c")


def random_rows(rng, n=250):
    rows, clock = [], {v: DAY for v in VEHICLES}
    for _ in range(n):
        v = rng.choice(VEHICLES)
        clock[v] += rng.choice((1, 30, 30, 30, 60, 89, 90, 91, 120, 400, 1000, 5000))
        coolant = rng.choice((90, 91, 90, 97, 98, 99, 80, 79, 120, None, 215))
        rpm = rng.choice((2000, 2000, 2000, 2600, None))
        r = row(clock[v], coolant, vehicle=v, rpm=rpm)
        rows.append(r)
        p = rng.random()
        if p < 0.05:
            rows.append(r)
        elif p < 0.08:
            rows.append(row(clock[v], 95, vehicle=v))
        elif p < 0.11:
            rows.append(row(clock[v] - rng.randint(1, 500), 90, vehicle=v))
    return rows


def property_reference():
    out = []
    for v in VEHICLES:
        out += alternating(0, 40, vehicle=v)
    return out


def direction(assessment):
    if assessment.status == "normal":
        return 0
    if assessment.status in ("suspected_anomaly", "confirmed_anomaly"):
        above = {"above_reference", "above_absolute_limit"} & set(assessment.reasons)
        return 1 if above else -1
    return None


def oracle_status(history, index, config):
    """Brute-force re-derivation of the confirmation rule from the spec, by
    scanning backwards; independent of the detector's incremental state."""
    current = history[index]
    d = direction(current)
    if d in (None, 0):
        return current.status
    start = last = current.epoch_us
    for previous in reversed(history[:index]):
        if previous.reasons[:1] == ("conflicting_duplicate",):
            break
        dp = direction(previous)
        if dp is None:
            continue
        if dp != d or last - previous.epoch_us > config.run_gap_s * 1e6:
            break
        start = last = previous.epoch_us
    held = (current.epoch_us - start) / 1e6
    return "confirmed_anomaly" if held >= config.confirm_s else "suspected_anomaly"


class PropertyTests(unittest.TestCase):
    def test_confirmation_matches_independent_oracle_and_invariants_hold(self):
        cfg = PROPERTY_CONFIG
        for seed in range(40):
            rng = random.Random(seed)
            rows = random_rows(rng)
            result = run(property_reference(), rows, cfg)
            self.assertEqual(len(result), len(rows))
            by_vehicle = {}
            for assessment in result:
                self.assertIn(assessment.status, STATUSES)
                if assessment.status != "normal":
                    self.assertTrue(assessment.reasons, assessment)
                if assessment.status == "normal":
                    self.assertLess(abs(assessment.delta_c), assessment.threshold_c)
                    self.assertLess(assessment.coolant_temp_c, cfg.absolute_high_c)
                    self.assertNotIn("warmup_transient", assessment.quality)
                if "above_reference" in assessment.reasons:
                    self.assertGreaterEqual(assessment.delta_c, assessment.threshold_c)
                if "below_reference" in assessment.reasons:
                    self.assertLessEqual(assessment.delta_c, -assessment.threshold_c)
                if "above_absolute_limit" in assessment.reasons:
                    self.assertGreaterEqual(assessment.coolant_temp_c, cfg.absolute_high_c)
                if assessment.vehicle_id is not None and assessment.epoch_us is not None:
                    by_vehicle.setdefault(assessment.vehicle_id, []).append(assessment)
            for history in by_vehicle.values():
                for i, assessment in enumerate(history):
                    with self.subTest(seed=seed, line=assessment.line):
                        self.assertEqual(assessment.status, oracle_status(history, i, cfg))


class MetamorphicTests(unittest.TestCase):
    @staticmethod
    def essence(assessments):
        return [(a.vehicle_id, a.epoch_us, a.status, a.reasons, a.delta_c) for a in assessments]

    def test_vehicle_isolation(self):
        for seed in range(15):
            rows = random_rows(random.Random(seed))
            full = [a for a in run(property_reference(), rows, PROPERTY_CONFIG) if a.vehicle_id == "a"]
            only_a = run([r for r in property_reference() if r[1] == "a"],
                         [r for r in rows if r[1] == "a"], PROPERTY_CONFIG)
            self.assertEqual(self.essence(full), self.essence(only_a), seed)

    def test_timezone_re_expression_is_invariant(self):
        def shift(rows, hours):
            from pexislink.ingest import parse_timestamp
            from datetime import timedelta, timezone
            out = []
            for r in rows:
                parsed = parse_timestamp(r[0])
                text = parsed[0].astimezone(timezone(timedelta(hours=hours))).isoformat() if parsed else r[0]
                out.append((text,) + tuple(r[1:]))
            return out
        rows = random_rows(random.Random(7))
        base = self.essence(run(property_reference(), rows, PROPERTY_CONFIG))
        for hours in (-3, 5, 14):
            shifted = run(shift(property_reference(), hours), shift(rows, hours), PROPERTY_CONFIG)
            self.assertEqual(self.essence(shifted), base, hours)

    def test_exact_duplicates_and_late_rows_do_not_change_other_outputs(self):
        rows = steady(DAY, 40, lambda i: 104 if 10 <= i < 25 else 90 + i % 2)
        clean = run(REFERENCE, rows)
        noisy_rows, original = [], []
        for i, r in enumerate(rows):
            original.append(len(noisy_rows))
            noisy_rows.append(r)
            if i % 4 == 1:
                noisy_rows.append(r)                                   # exact duplicate
            if i % 7 == 3:
                noisy_rows.append(row(DAY + 30 * i - 45, 150))         # late row
        noisy = run(REFERENCE, noisy_rows)
        self.assertEqual(self.essence([noisy[i] for i in original]), self.essence(clean))

    def test_determinism(self):
        rows = random_rows(random.Random(3))
        self.assertEqual(run(property_reference(), rows, PROPERTY_CONFIG),
                         run(property_reference(), rows, PROPERTY_CONFIG))


if __name__ == "__main__":
    unittest.main()
