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
        self.assertEqual(lines[-1]["reasons"], ["coolant_temp_c_sentinel"])
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
        self.assertIn("line 42: unterminated quoted field", err)

    def test_reference_overlap_is_reported_per_row_not_fatal(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        overlapping = self.write("obs.csv", csv_text(alternating(DAY, 10) + [row(600, 90)]))
        code, out, err = self.call("detect", "--reference", reference, "--observations", overlapping)
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(lines), 11)
        self.assertEqual(lines[-1]["reasons"], ["reference_overlap"])
        self.assertIn("warning: 1 observation row(s)", err)

    def test_closed_schema_errors_are_fatal(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        extra = self.write("extra.csv", csv_text([row(DAY, 90) + ("x",)], HEADER + ",latitude"))
        code, out, err = self.call("detect", "--reference", reference, "--observations", extra)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("too many CSV columns", err)

    def test_summary_destination_is_checked_before_any_output(self):
        """Code review L2: the summary used to fail only after all output, and could
        silently overwrite an input file."""
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        observed = self.write("obs.csv", csv_text(alternating(DAY, 5)))
        for destination, message in ((observed, "refusing to overwrite an input"),
                                     (str(self.dir / "missing" / "s.json"), "No such file")):
            code, out, err = self.call("detect", "--reference", reference, "--observations", observed,
                                       "--summary", destination)
            self.assertEqual((code, out), (2, ""), destination)
            self.assertIn(message, err)
        self.assertTrue(Path(observed).read_text().startswith("timestamp"))

    def test_numeric_flags_are_bounded(self):
        reference = self.write("ref.csv", csv_text(alternating(0, 80)))
        for flag in ("99999999999999999999", "0", "100000000"):
            with self.assertRaises(SystemExit) as caught, redirect_stderr(io.StringIO()):
                main(["detect", "--reference", reference, "--observations", reference, "--max-file-mb", flag])
            self.assertEqual(caught.exception.code, 2)

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

    def test_legacy_accepts_a_file_size_flag(self):
        code, out, _ = self.call("--baseline", str(ROOT / "examples/baseline.csv"),
                                 "--observations", str(ROOT / "examples/observations.csv"), "--max-file-mb", "1")
        self.assertEqual((code, len(out.splitlines())), (0, 3))

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

    @unittest.skipUnless(os.path.exists("/dev/full"), "requires /dev/full")
    def test_output_device_full_exits_1_without_traceback(self):
        # Both buffering modes: with buffered stdout (the default, and CI's) the
        # interpreter flushed the leftover buffer again at exit and exited 120.
        for unbuffered in (None, "1"):
            env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
            env["PYTHONPATH"] = str(ROOT / "src")
            if unbuffered:
                env["PYTHONUNBUFFERED"] = unbuffered
            with self.subTest(PYTHONUNBUFFERED=unbuffered), open("/dev/full", "w") as full:
                proc = subprocess.run([sys.executable, "-m", "pexislink", "detect",
                                       "--reference", str(ROOT / "examples/baseline.csv"),
                                       "--observations", str(ROOT / "examples/observations.csv")],
                                      stdout=full, stderr=subprocess.PIPE, text=True, env=env, timeout=60)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertNotIn("Exception ignored", proc.stderr)

    @unittest.skipUnless(os.path.exists("/dev/full"), "requires /dev/full")
    def test_every_command_exits_1_when_stdout_cannot_be_written(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
        env["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as tmp:
            commands = {
                "version": ["--version"],
                "legacy": ["--baseline", str(ROOT / "examples/baseline.csv"),
                           "--observations", str(ROOT / "examples/observations.csv")],
                "synth": ["synth", "--scenario", "normal_operation", "--seed", "1", "--out", tmp],
            }
            for name, argv in commands.items():
                with self.subTest(command=name), open("/dev/full", "w") as full:
                    proc = subprocess.run([sys.executable, "-m", "pexislink", *argv], stdout=full,
                                          stderr=subprocess.PIPE, text=True, env=env, timeout=120)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    self.assertIn("cannot write output", proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertNotIn("Exception ignored", proc.stderr)


if __name__ == "__main__":
    unittest.main()
