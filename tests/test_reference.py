"""Reference model: robust statistics, exclusions, context keys, leakage guard."""

import unittest
from dataclasses import replace

from helpers import FOCUSED, alternating, records, row, steady

from pexislink.config import DetectorConfig
from pexislink.detect import assess_records
from pexislink.ingest import InputError
from pexislink.reference import build_reference, check_chronology, robust_stats

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
    def test_overlap_is_rejected_using_every_placeable_reference_row(self):
        ref = model(alternating(0, 10) + [row(10_000, 215)])  # implausible row still counts
        with self.assertRaisesRegex(InputError, "leakage"):
            check_chronology(ref, records([row(9_000, 90)]))
        with self.assertRaisesRegex(InputError, "leakage"):  # second, independent barrier
            list(assess_records(ref, records([row(10_000, 90)])))

    def test_vehicles_without_reference_are_unconstrained(self):
        ref = model(alternating(DAY, 10))
        check_chronology(ref, records([row(0, 90, vehicle="new")]))


class DefaultConfigTests(unittest.TestCase):
    def test_defaults_are_the_pre_registered_values(self):
        cfg = DetectorConfig()
        self.assertEqual((cfg.warmup_s, cfg.k_scale, cfg.confirm_s, cfg.run_gap_s,
                          cfg.absolute_high_c, cfg.min_reference_samples),
                         (900.0, 4.0, 300.0, 90.0, 115.0, 30))

    def test_invalid_configurations_are_rejected(self):
        bad = [dict(confirm_s=60, run_gap_s=90), dict(k_scale=float("nan")), dict(warmup_s=-1),
               dict(coolant_range_c=(150.0, -40.0)), dict(use_load=1), dict(min_reference_samples=1),
               dict(absolute_high_c=0), dict(run_gap_s=1000, segment_gap_s=900, confirm_s=2000)]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DetectorConfig(**kwargs)

    def test_json_configuration_is_a_closed_schema(self):
        self.assertEqual(DetectorConfig.from_dict({"coolant_range_c": [-30, 140]}).coolant_range_c,
                         (-30, 140))
        with self.assertRaisesRegex(ValueError, "unknown configuration key"):
            DetectorConfig.from_dict({"k_scal": 3})


if __name__ == "__main__":
    unittest.main()
