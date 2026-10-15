"""End-to-end CLI behaviour, including fail-closed output and the legacy path."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import alternating, row

from pexislink import __version__
from pexislink.cli import main

ROOT = Path(__file__).resolve().parents[1]
HEADER = "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c,engine_load_pct"
DAY = 86_400


def csv_text(rows, header=HEADER):
    return header + "\n" + "".join(",".join(r) + "\n" for r in rows)


class CliFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()


class DetectCommandTests(CliFixture):
    def test_jsonl_output_and_summary(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        observed = self.write("obs.csv", csv_text(alternating(DAY, 40) + [row(DAY + 1200, 215)]))
        summary_path = self.dir / "summary.json"
        code, out, err = self.call("detect", "--reference", reference, "--observations", observed,
                                   "--summary", str(summary_path))
        self.assertEqual((code, err), (0, ""))
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(lines), 41)
        self.assertEqual({line["schema"] for line in lines}, {"pexislink.assessment.v2"})
        self.assertEqual(lines[0]["reasons"], ["warmup_transient"])
        self.assertEqual(lines[-1]["reasons"], ["coolant_temp_c_implausible"])
        summary = json.loads(summary_path.read_text())
        self.assertEqual(summary["observations"]["rows"], 41)
        self.assertEqual(len(summary["inputs"]["observations"]["sha256"]), 64)
        self.assertEqual(summary["inputs"]["reference"]["file"], "ref.csv")  # no directory leakage
        self.assertTrue(summary["reference_model"]["uses_engine_load"])
        self.assertIn("not a diagnosed fault", summary["notice"])

    def test_fails_closed_without_partial_output_when_last_line_is_corrupt(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        corrupt = csv_text(alternating(DAY, 40)) + '2026-10-03T08:00:00Z,"v1,2000,60,90,30\n'
        code, out, err = self.call("detect", "--reference", reference,
                                   "--observations", self.write("obs.csv", corrupt))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("malformed CSV", err)

    def test_leakage_and_closed_schema_errors_are_fatal(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        overlapping = self.write("obs.csv", csv_text(alternating(DAY, 10) + [row(600, 90)]))
        code, out, err = self.call("detect", "--reference", reference, "--observations", overlapping)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("leakage", err)
        extra = self.write("extra.csv", csv_text([row(DAY, 90) + ("x",)], HEADER + ",latitude"))
        code, out, err = self.call("detect", "--reference", reference, "--observations", extra)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("closed schema", err)

    def test_configuration_file(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        observed = self.write("obs.csv", csv_text(alternating(DAY, 5)))
        good = self.write("good.json", json.dumps({"warmup_s": 0, "min_reference_samples": 5}))
        code, out, _ = self.call("detect", "--reference", reference, "--observations", observed,
                                 "--config", good)
        self.assertEqual(code, 0)
        self.assertEqual({json.loads(line)["status"] for line in out.splitlines()}, {"normal"})
        bad = self.write("bad.json", json.dumps({"warmup": 0}))
        code, out, err = self.call("detect", "--reference", reference, "--observations", observed,
                                   "--config", bad)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown configuration key", err)

    def test_vehicle_limit(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        many = self.write("obs.csv", csv_text([row(DAY, 90, vehicle=f"v{i}") for i in range(5)]))
        code, out, err = self.call("detect", "--reference", reference, "--observations", many,
                                   "--max-vehicles", "3")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("more than 3 vehicles", err)

    def test_v01_example_files_are_honestly_unassessable_under_v02_evidence_rules(self):
        code, out, _ = self.call("detect", "--reference", str(ROOT / "examples/baseline.csv"),
                                 "--observations", str(ROOT / "examples/observations.csv"))
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["reasons"] for line in out.splitlines()],
                         [["warmup_transient"]] * 3)


class LegacyAndTopLevelTests(CliFixture):
    def test_legacy_invocation_output_is_unchanged_and_warns(self):
        code, out, err = self.call("--baseline", str(ROOT / "examples/baseline.csv"),
                                   "--observations", str(ROOT / "examples/observations.csv"))
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["status"] for line in out.splitlines()],
                         ["normal", "suspected_anomaly", "anomaly"])
        self.assertIn("deprecated", err)

    def test_legacy_structural_error_exits_2_instead_of_traceback(self):
        big = self.write("big.csv", "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c\n"
                         "2026-10-02T08:00:00Z," + "A" * 200_000 + ",2000,60,90\n")
        code, out, err = self.call("--baseline", big, "--observations", big)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("malformed CSV", err)

    def test_help_version_and_usage(self):
        self.assertEqual(self.call("--help")[0], 0)
        self.assertEqual(self.call()[0], 2)
        self.assertEqual(self.call("--version")[1].strip(), __version__)


class SubprocessTests(unittest.TestCase):
    def test_real_process_exit_codes(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        ok = subprocess.run([sys.executable, "-m", "pexislink", "detect",
                             "--reference", str(ROOT / "examples/baseline.csv"),
                             "--observations", str(ROOT / "examples/observations.csv")],
                            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        missing = subprocess.run([sys.executable, "-m", "pexislink", "detect",
                                  "--reference", "/nonexistent.csv", "--observations", "/nonexistent.csv"],
                                 capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr)


if __name__ == "__main__":
    unittest.main()
