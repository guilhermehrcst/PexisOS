"""Reference model: robust statistics, exclusions, context keys, leakage guard."""

import unittest
from dataclasses import replace

from helpers import FOCUSED, alternating, records, row, steady

from pexislink.config import DetectorConfig
from pexislink.detect import assess_records
from pexislink.ingest import InputError
from pexislink.reference import build_reference, robust_stats

DAY = 86_400


def model(rows, config=FOCUSED, use_load=False):
    return build_reference(records(rows), config, use_load=use_load)


class RobustStatsTests(unittest.TestCase):
    def test_median_and_mad_scale(self):
        stats = robust_stats([88, 89, 90, 91, 92, 150], scale_floor_c=0.1)
        self.assertEqual(stats.median_c, 90.5)
        self.assertAlmostEqual(stats.scale_c, 1.4826 * 1.5)  # |dev| = 2.5,1.5,.5,.5,1.5,59.5

    def test_quantised_constant_data_cannot_produce_zero_scale(self):
        self.assertEqual(robust_stats([90] * 50, scale_floor_c=1.0).scale_c, 1.0)


class ScaleTests(unittest.TestCase):
    """F-06 counterpart: the threshold follows the context's own spread."""

    def statuses(self, reference_rows, observed):
        ref = model(reference_rows)
        return [a.status for a in assess_records(ref, records(observed))]

    def test_plus_6_5c_in_a_tight_context_is_detected(self):
        tight = alternating(0, 40)        # 90/91 quantised: median 90.5, scale floored to 1 C
        observed = steady(DAY, 12, 97)    # +6.5 C >= 4 * 1 C (v0.1 needed 8 C)
        self.assertEqual(self.statuses(tight, observed)[-1], "confirmed_anomaly")

    def test_plus_9c_in_a_wide_context_is_normal(self):
        wide = [row(30 * i, (80, 100, 84, 96, 90, 88)[i % 6]) for i in range(60)]
        observed = [row(DAY + 30 * i, (99, 98)[i % 2]) for i in range(12)]
        self.assertEqual(set(self.statuses(wide, observed)), {"normal"})


class ExclusionTests(unittest.TestCase):
    def test_reference_uses_only_clean_warm_plausible_samples(self):
        cfg = replace(FOCUSED, warmup_s=300.0, min_reference_samples=2)
        rows = ([row(0, 40), row(30, 60)]                     # warm-up transient
                + alternating(600, 6)                          # usable
                + [row(800, 215), row(830, 120), row(860, None)]  # implausible, >= abs limit, missing
                + [row(860, None), row(890, 91, rpm=None)])    # duplicate, missing context
        ref = model(rows, cfg)
        self.assertEqual(ref.used, 6)
        self.assertEqual(ref.excluded, {"warmup_transient": 2, "coolant_unusable": 2,
                                        "above_absolute_limit": 1, "duplicate": 1,
                                        "context_unusable": 1})

    def test_context_below_minimum_count_is_unusable(self):
        ref = model(alternating(0, 4), replace(FOCUSED, min_reference_samples=5))
        self.assertEqual(len(ref.stats), 0)
        self.assertEqual(ref.vehicles, frozenset())

    def test_load_joins_the_context_and_100_percent_shares_the_top_bin(self):
        rows = [row(30 * i, 90 + i % 2, load=(76 if i % 2 else 100)) for i in range(10)]
        ref = model(rows, use_load=True)
        self.assertEqual(list(ref.stats), [("v1", 4, 3, 3)])


class ChronologyTests(unittest.TestCase):
    """Leakage boundary: the last reference sample that contributed to the model."""

    def test_overlapping_observations_are_quarantined_per_record(self):
        ref = model(alternating(0, 10))
        result = list(assess_records(ref, records([row(200, 90), row(DAY, 90), row(DAY + 30, 91)])))
        self.assertEqual(result[0].reasons, ("reference_overlap",))
        self.assertEqual([a.status for a in result[1:]], ["normal", "normal"])

    def test_excluded_reference_glitch_cannot_deny_a_vehicle(self):
        """Code review M3: one 2199-dated reference row used to abort the whole run.
        With the default warm-up the lone future row starts a segment, is excluded,
        and therefore cannot move the boundary. (A glitched row that does contribute
        moves it, and that vehicle's earlier observations are quarantined.)"""
        cfg = DetectorConfig(min_reference_samples=5)
        glitch = ("2199-01-01T00:00:00Z", "v1", "2000", "60", "90", "30")
        clean = alternating(0, 40)
        ref = model(clean + [glitch], cfg)
        self.assertEqual(ref.reference_end_us["v1"], records(clean)[-1].epoch_us)
        result = list(assess_records(ref, records(alternating(DAY, 40))))
        self.assertNotIn(("reference_overlap",), {a.reasons for a in result})
        self.assertEqual(result[-1].status, "normal")

    def test_vehicles_without_reference_are_unconstrained(self):
        ref = model(alternating(DAY, 10))
        (assessment,) = assess_records(ref, records([row(0, 90, vehicle="new")]))
        self.assertEqual(assessment.reasons, ("no_reference_vehicle",))

    def test_context_limit(self):
        rows = [row(30 * i, 90, rpm=500 * i) for i in range(20)]
        with self.assertRaisesRegex(InputError, "more than 5 reference contexts"):
            build_reference(records(rows), FOCUSED, use_load=False, max_contexts=5)


class DefaultConfigTests(unittest.TestCase):
    def test_defaults_are_the_pre_registered_values(self):
        cfg = DetectorConfig()
        self.assertEqual((cfg.warmup_s, cfg.k_scale, cfg.confirm_s, cfg.run_gap_s,
                          cfg.absolute_high_c, cfg.min_reference_samples),
                         (900.0, 4.0, 300.0, 90.0, 115.0, 30))

    def test_invalid_configurations_are_rejected(self):
        bad = [dict(confirm_s=60, run_gap_s=90), dict(k_scale=float("nan")), dict(warmup_s=-1),
               dict(coolant_range_c=(150.0, -40.0)), dict(use_load=1), dict(min_reference_samples=1),
               dict(absolute_high_c=0), dict(run_gap_s=1000, segment_gap_s=900, confirm_s=2000),
               # code review L1, L4, L9
               dict(absolute_high_c=160, coolant_range_c=(-40.0, 150.0)), dict(k_scale=10 ** 400),
               dict(segment_gap_s=1e303), dict(rpm_bin=1e-320), dict(k_scale=1e308),
               dict(run_gap_s=90.0000006, confirm_s=90.0000007), dict(coolant_sentinels_c=(float("inf"),))]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DetectorConfig(**kwargs)

    def test_json_configuration_is_a_closed_schema(self):
        self.assertEqual(DetectorConfig.from_dict({"coolant_range_c": [-30, 140]}).coolant_range_c,
                         (-30, 140))
        with self.assertRaisesRegex(ValueError, "unknown configuration key"):
            DetectorConfig.from_dict({"k_scal": 3})

    def test_json_file_hazards_are_value_errors(self):
        import os
        import tempfile
        cases = {'{"k_scale": 3, "k_scale": 4}': "duplicate configuration key",
                 "[" * 100_000 + "]" * 100_000: "nested too deeply"}
        for text, message in cases.items():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                handle.write(text)
            self.addCleanup(os.unlink, handle.name)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                DetectorConfig.from_json_file(handle.name)


if __name__ == "__main__":
    unittest.main()
