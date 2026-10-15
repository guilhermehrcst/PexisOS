import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from pexislink import Sample, analyze, read_samples
from pexislink.cli import main


ROOT = Path(__file__).resolve().parents[1]


def sample(day, minute, temp, *, vehicle="demo-001", rpm=2000, speed=60):
    return Sample.from_mapping({
        "timestamp": f"2026-10-{day:02d}T08:{minute:02d}:00Z",
        "vehicle_id": vehicle,
        "engine_rpm": str(rpm),
        "speed_kmh": str(speed),
        "coolant_temp_c": str(temp),
    })


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.baseline = [sample(1, n, 90 + (n % 2)) for n in range(6)]

    def test_sustained_deviation_is_anomaly_not_prediction(self):
        observed = [sample(2, 0, 91), sample(2, 1, 103), sample(2, 2, 104)]
        result = analyze(self.baseline, observed)
        self.assertEqual([a.status for a in result], [
            "normal", "suspected_anomaly", "anomaly",
        ])
        self.assertEqual(result[-1].reference_c, 90.5)

    def test_unknown_vehicle_is_not_considered_healthy(self):
        result = analyze(self.baseline, [sample(2, 0, 150, vehicle="new")])
        self.assertEqual(result[0].status, "insufficient_baseline")
        self.assertIsNone(result[0].delta_c)

    def test_unseen_operating_context_is_unassessable(self):
        result = analyze(self.baseline, [sample(2, 0, 120, rpm=5000)])
        self.assertEqual(result[0].status, "insufficient_baseline")

    def test_direction_change_does_not_complete_persistence(self):
        result = analyze(self.baseline, [sample(2, 0, 102), sample(2, 1, 78)])
        self.assertEqual([a.status for a in result], [
            "suspected_anomaly", "suspected_anomaly",
        ])

    def test_rejects_future_baseline_to_avoid_leakage(self):
        with self.assertRaisesRegex(ValueError, "strictly earlier"):
            analyze(self.baseline, [sample(1, 4, 92)])

    def test_rejects_out_of_order_observations(self):
        with self.assertRaisesRegex(ValueError, "strictly increase"):
            analyze(self.baseline, [sample(2, 2, 90), sample(2, 1, 91)])

    def test_rejects_nonfinite_sensor_readings(self):
        row = {"timestamp": "2026-10-02T08:00:00Z", "vehicle_id": "v",
               "engine_rpm": "2000", "speed_kmh": "60", "coolant_temp_c": "nan"}
        with self.assertRaisesRegex(ValueError, "finite"):
            Sample.from_mapping(row)

    def test_requires_timezone(self):
        row = {"timestamp": "2026-10-02T08:00:00", "vehicle_id": "v",
               "engine_rpm": "2000", "speed_kmh": "60", "coolant_temp_c": "90"}
        with self.assertRaisesRegex(ValueError, "timezone"):
            Sample.from_mapping(row)

    def test_rejects_invalid_configuration(self):
        with self.assertRaisesRegex(ValueError, "threshold_c"):
            analyze(self.baseline, [], threshold_c=float("nan"))

    def test_csv_missing_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.csv"
            path.write_text("timestamp,vehicle_id\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing CSV columns"):
                read_samples(path)

    def test_rejects_unterminated_quoted_csv_field(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "truncated.csv"
            path.write_text(
                "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c\\n"
                '2026-10-02T08:00:00Z,demo-001,2000,60,"90\\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "malformed CSV"):
                read_samples(path)

    def test_bucket_jitter_is_known_v01_limitation(self):
        # Historical v0.1 comparator: bucket transitions reset sample streaks.
        # v0.2 replaces this with a duration-based evidence rule.
        baseline = [sample(1, i, 90, rpm=1999) for i in range(5)]
        baseline += [sample(1, i + 5, 90, rpm=2000) for i in range(5)]
        observations = [sample(2, 0, 104, rpm=1999),
                        sample(2, 1, 104, rpm=2000)]
        self.assertEqual(
            [a.status for a in analyze(baseline, observations)],
            ["suspected_anomaly", "suspected_anomaly"],
        )

    def test_cli_prints_three_json_lines(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([
                "--baseline", str(ROOT / "examples/baseline.csv"),
                "--observations", str(ROOT / "examples/observations.csv"),
            ])
        self.assertEqual(result, 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-1]["status"], "anomaly")


if __name__ == "__main__":
    unittest.main()
