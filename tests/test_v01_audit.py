"""Executable evidence for the v0.1 audit (docs/AUDIT-v0.1.md).

The v0.1 decision rule in ``pexislink.core.analyze`` is deliberately frozen: it
is the comparator in the benchmark, so changing it would silently invalidate
every v0.1-vs-v0.2 comparison. Tests named ``test_F0x_*`` therefore pin the
*observed defective behaviour* of v0.1 (characterisation tests). Each one names
the v0.2 test that asserts the corrected behaviour.

Exceptions are the input-contract defects F-09, F-10 and F-11, which were fixed
in ``core.read_samples`` itself (crash / silent misread / resource exhaustion
are never acceptable, even in a legacy path); those tests assert the fix.
"""

import csv
import os
import tempfile
import unittest
from pathlib import Path

from pexislink import Sample, analyze, read_samples

HEADER = "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c\n"


def sample(day, minute, temp, *, vehicle="v1", rpm=2000, speed=60):
    return Sample.from_mapping({
        "timestamp": f"2026-10-{day:02d}T08:{minute:02d}:00Z",
        "vehicle_id": vehicle,
        "engine_rpm": str(rpm),
        "speed_kmh": str(speed),
        "coolant_temp_c": str(temp),
    })


def statuses(assessments):
    return [a.status for a in assessments]


class FrozenV01DefectCharacterisation(unittest.TestCase):
    """Pins known v0.1 defects. A failure here means the comparator changed."""

    def setUp(self):
        self.baseline = [sample(1, n, 90 + (n % 2)) for n in range(6)]

    def test_F01_stuck_sensor_reported_normal_indefinitely(self):
        """F-01: a frozen sensor at a plausible value yields 'normal' forever.

        Silent false reassurance. v0.2: test_detect.StuckSensorTests.
        """
        frozen = [sample(2 + day, minute, 90) for day in range(20) for minute in range(0, 60, 10)]
        self.assertEqual(set(statuses(analyze(self.baseline, frozen))), {"normal"})

    def test_F02_normal_cold_start_is_confirmed_anomaly(self):
        """F-02: an ordinary warm-up ramp is reported as a confirmed anomaly.

        v0.2: test_detect.WarmupTests.
        """
        ramp = [sample(2, m, t) for m, t in zip(range(8), (25, 35, 45, 55, 65, 75, 82, 88))]
        self.assertEqual(statuses(analyze(self.baseline, ramp)).count("anomaly"), 6)

    def test_F02_legitimate_load_increase_is_confirmed_anomaly(self):
        """F-02: +9.5 C from heavier load at identical rpm/speed -> anomaly.

        v0.2 uses engine_load_pct when available: test_detect.LoadContextTests.
        """
        towing = [sample(2, m, 99.5) for m in range(3)]
        self.assertEqual(statuses(analyze(self.baseline, towing)),
                         ["suspected_anomaly", "anomaly", "anomaly"])

    def test_F03_persistence_bridges_a_28_day_gap(self):
        """F-03: two deviations 28 days apart become a 'sustained' anomaly.

        v0.2: test_detect.PersistenceTests.test_run_does_not_bridge_gaps.
        """
        result = analyze(self.baseline, [sample(2, 0, 103), sample(30, 0, 103)])
        self.assertEqual(statuses(result), ["suspected_anomaly", "anomaly"])

    def test_F03_two_samples_100ms_apart_confirm_anomaly(self):
        """F-03: persistence counts samples, so its meaning depends on sample rate.

        v0.2: test_detect.PersistenceTests.test_confirmation_requires_duration.
        """
        rows = [Sample.from_mapping({
            "timestamp": f"2026-10-02T08:00:00.{ms:03d}Z", "vehicle_id": "v1",
            "engine_rpm": "2000", "speed_kmh": "60", "coolant_temp_c": "104"})
            for ms in (0, 100)]
        self.assertEqual(statuses(analyze(self.baseline, rows)),
                         ["suspected_anomaly", "anomaly"])

    def test_F04_sensor_sentinels_are_confirmed_anomalies(self):
        """F-04: 215 C / -40 C sensor sentinels and impossible values are accepted.

        v0.2: test_quality.PlausibilityTests.
        """
        for sentinel in (215, -40):
            result = analyze(self.baseline, [sample(2, 0, sentinel), sample(2, 1, sentinel)])
            self.assertEqual(statuses(result), ["suspected_anomaly", "anomaly"])
        absurd = Sample.from_mapping({
            "timestamp": "2026-10-02T08:00:00Z", "vehicle_id": "v1",
            "engine_rpm": "1e300", "speed_kmh": "1e300", "coolant_temp_c": "-1000"})
        self.assertEqual(absurd.coolant_temp_c, -1000.0)  # below absolute zero, accepted

    def test_F05_bucket_edge_jitter_prevents_confirmation(self):
        """F-05: 10 consecutive +14 C deviations never confirm when rpm jitters
        across a 500-rpm bucket edge, because the streak is keyed by context.

        v0.2: test_detect.PersistenceTests.test_context_change_keeps_run.
        """
        baseline = ([sample(1, n, 90, rpm=1990) for n in range(6)]
                    + [sample(1, 10 + n, 90, rpm=2010) for n in range(6)])
        jitter = [sample(2, n, 104, rpm=1990 if n % 2 == 0 else 2010) for n in range(10)]
        self.assertEqual(set(statuses(analyze(baseline, jitter))), {"suspected_anomaly"})

    def test_F05_one_unassessable_sample_resets_persistence(self):
        """F-05: a single brief excursion to an unseen context resets the streak.

        v0.2: test_detect.PersistenceTests.test_unassessable_sample_is_neutral.
        """
        rows = [sample(2, 0, 104), sample(2, 1, 104, rpm=5000), sample(2, 2, 104)]
        self.assertEqual(statuses(analyze(self.baseline, rows)),
                         ["suspected_anomaly", "insufficient_baseline", "suspected_anomaly"])

    def test_F06_absolute_threshold_ignores_dispersion(self):
        """F-06: +7 C in a zero-spread context is 'normal'; noise in a wide
        context is a confirmed anomaly. v0.2: test_reference.ScaleTests.
        """
        tight = [sample(1, n, 90) for n in range(6)]
        self.assertEqual(set(statuses(analyze(tight, [sample(2, n, 97) for n in range(5)]))),
                         {"normal"})
        noisy = [sample(1, n, v) for n, v in enumerate((80, 100, 84, 96, 90, 88))]
        self.assertEqual(statuses(analyze(noisy, [sample(2, 0, 99), sample(2, 1, 98)])),
                         ["suspected_anomaly", "anomaly"])

    def test_F07_fault_in_reference_period_is_learned_as_normal(self):
        """F-07: a vehicle already overheating during the reference period is
        'normal' afterwards. Fundamental to self-referenced detection; v0.2 adds
        a reference-free absolute barrier: test_detect.AbsoluteBarrierTests.
        """
        hot = [sample(1, n, 112 + (n % 2)) for n in range(6)]
        self.assertEqual(statuses(analyze(hot, [sample(2, 0, 112), sample(2, 1, 113)])),
                         ["normal", "normal"])

    def test_F08_one_duplicate_row_aborts_the_whole_analysis(self):
        """F-08: one exact duplicate (routine in telemetry) aborts everything.

        v0.2 quarantines it per record: test_quality.IntegrityTests.
        """
        rows = [sample(2, 0, 90), sample(2, 1, 90), sample(2, 1, 90), sample(2, 2, 90)]
        with self.assertRaisesRegex(ValueError, "strictly increase"):
            analyze(self.baseline, rows)

    def test_F08_one_late_row_also_denies_results_for_other_vehicles(self):
        """F-08: fail-closed at fleet granularity instead of record granularity."""
        rows = [sample(2, 0, 90, vehicle="other"), sample(2, 1, 90), sample(2, 0, 90)]
        with self.assertRaisesRegex(ValueError, "strictly increase"):
            analyze(self.baseline, rows)

    def test_F12_lenient_numerals_and_identifiers(self):
        """F-12: open schema. Underscores, non-ASCII digits and control
        characters are accepted; NFC/NFD spellings split one vehicle in two.

        v0.2 closes the schema: test_ingest.RowSyntaxTests.
        """
        row = {"timestamp": "2026-10-02T08:00:00Z", "vehicle_id": "v\x1b[31m",
               "engine_rpm": "2_000", "speed_kmh": "٦٠", "coolant_temp_c": "９０"}
        parsed = Sample.from_mapping(row)
        self.assertEqual((parsed.engine_rpm, parsed.speed_kmh, parsed.coolant_temp_c),
                         (2000.0, 60.0, 90.0))
        nfc = [sample(1, n, 90, vehicle="José") for n in range(6)]
        result = analyze(nfc, [sample(2, 0, 90, vehicle="José")])
        self.assertEqual(statuses(result), ["insufficient_baseline"])


class FrozenV01VerifiedStrengths(unittest.TestCase):
    """Behaviour the audit tried to break and could not. Preserved in v0.2."""

    def setUp(self):
        self.baseline = [sample(1, n, 90 + (n % 2)) for n in range(6)]

    def test_S1_vehicle_state_isolation_under_interleaving(self):
        observed = [sample(2, n, 104) for n in range(3)]
        alone = statuses(analyze(self.baseline, observed))
        other = [sample(1, n, 60, vehicle="B") for n in range(6)]
        mixed = []
        for n in range(3):
            mixed += [sample(2, n, 140, vehicle="B"), observed[n]]
        interleaved = [a.status for a in analyze(self.baseline + other, mixed)
                       if a.sample.vehicle_id == "v1"]
        self.assertEqual(alone, interleaved)

    def test_S2_mixed_utc_offsets_are_ordered_by_instant(self):
        def at(text):
            return Sample.from_mapping({"timestamp": text, "vehicle_id": "v1",
                                        "engine_rpm": "2000", "speed_kmh": "60",
                                        "coolant_temp_c": "104"})
        rows = [at("2026-10-02T08:00:00+00:00"), at("2026-10-02T10:00:30+02:00"),
                at("2026-10-02T05:01:00-03:00")]
        self.assertEqual(statuses(analyze(self.baseline, rows)),
                         ["suspected_anomaly", "anomaly", "anomaly"])

    def test_S3_overlapping_reference_and_observation_rejected(self):
        with self.assertRaisesRegex(ValueError, "strictly earlier"):
            analyze(self.baseline, [self.baseline[-1]])


class FixedV01InputContract(unittest.TestCase):
    """F-09, F-10, F-11: fixed in core.read_samples (TDD: these were red first)."""

    def write(self, content: bytes) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            handle.write(content)
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_F09_csv_structure_error_is_reported_as_value_error(self):
        """Was: uncaught _csv.Error -> traceback and exit code 1 in the CLI."""
        oversized = HEADER + "2026-10-02T08:00:00Z," + "A" * 200_000 + ",2000,60,90\n"
        path = self.write(oversized.encode())
        with self.assertRaises(ValueError) as caught:
            read_samples(path)
        self.assertNotIsInstance(caught.exception, csv.Error)

    def test_F11_duplicate_header_is_rejected(self):
        """Was: the last duplicate column silently won (read 150 instead of 90)."""
        path = self.write(b"timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c,coolant_temp_c\n"
                          b"2026-10-02T08:00:00Z,v1,2000,60,90,150\n")
        with self.assertRaisesRegex(ValueError, "duplicate CSV column"):
            read_samples(path)

    def test_F10_input_size_is_bounded(self):
        """Was: whole input materialised (~7x file size); unbounded line reads."""
        path = self.write((HEADER + "2026-10-02T08:00:00Z,v1,2000,60,90\n" * 50).encode())
        with self.assertRaisesRegex(ValueError, "exceeds"):
            read_samples(path, max_bytes=256)

    @unittest.skipUnless(os.path.exists("/dev/zero"), "requires /dev/zero")
    def test_F10_endless_non_regular_input_is_bounded(self):
        """Was: MemoryError after exhausting RAM (probe P24)."""
        with self.assertRaisesRegex(ValueError, "exceeds"):
            read_samples("/dev/zero", max_bytes=1 << 20)


if __name__ == "__main__":
    unittest.main()
