"""v0.4 Lab A integration with the REAL cantools decoder (optional dependency).

Skipped, and reported as not run, when cantools is not installed. CI runs it in
the separate ``can-replay-cantools`` job with the pinned, hash-locked version in
requirements/lab-can.txt. Inputs are SYNTHETIC (canreplay_fixtures)."""

import importlib.util
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from canreplay_fixtures import CLEAN_ROWS, EXPECTED, LAYOUT, SYNTHETIC_DBC, oracle, stub_factory, write_inputs

from pexislink import canreplay, lab_cli

HAVE_CANTOOLS = importlib.util.find_spec("cantools") is not None
SRC = Path(__file__).resolve().parents[1] / "src"


@unittest.skipUnless(HAVE_CANTOOLS, "not_run: cantools is not installed (pip install -r requirements/lab-can.txt)")
class RealCantoolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dbc, self.frames = write_inputs(self.root / "in")
        self.out, self.summary = self.root / "records.jsonl", self.root / "summary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def run_real(self, dbc=None, frames=None):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = lab_cli.main(["can-replay", "--dbc", str(dbc or self.dbc), "--frames", str(frames or self.frames),
                                 "--out", str(self.out), "--summary", str(self.summary)])
        return code, stderr.getvalue()

    def test_pinned_version_is_the_one_under_test(self):
        import cantools
        self.assertEqual(cantools.__version__, canreplay.CANTOOLS_VERSION)

    def test_real_decoder_matches_the_hand_checked_values(self):
        code, stderr = self.run_real()
        self.assertEqual(code, 0, stderr)
        records = [json.loads(line) for line in self.out.read_text().splitlines()]
        self.assertEqual([r["status"] for r in records],
                         ["decoded", "decoded", "decoded", "unknown_message", "decode_error", "decoded"])
        std, ext = records[0], records[1]
        self.assertEqual({s["name"]: s["value"] for s in std["signals"]}, EXPECTED[("0x123", False, "1027FE340F000000")])
        self.assertEqual({s["name"]: s["value"] for s in ext["signals"]}, EXPECTED[("0x123", True, "7B008000")])
        self.assertEqual({s["name"]: s["unit"] for s in std["signals"]}, {"std_u16": "degC", "std_s8": None, "std_s12": "V"})
        summary = json.loads(self.summary.read_text())
        self.assertEqual(summary["decoder"], {"name": "cantools", "version": canreplay.CANTOOLS_VERSION})

    def test_real_and_stub_records_are_identical(self):
        """The stub used in standard-library CI behaves like cantools on the shared fixture."""
        self.run_real()
        real = self.out.read_bytes()
        with redirect_stderr(io.StringIO()):
            lab_cli.main(["can-replay", "--dbc", str(self.dbc), "--frames", str(self.frames), "--out", str(self.out),
                          "--summary", str(self.summary)], decoder_factory=stub_factory)
        self.assertEqual(real, self.out.read_bytes())

    def test_random_payloads_agree_with_the_independent_oracle(self):
        decoder = canreplay.load_cantools_decoder(SYNTHETIC_DBC, canreplay.ReplayLimits())
        rng = random.Random(4)
        for _ in range(2000):
            key = rng.choice([(0x123, False), (0x123, True)])
            data = bytes(rng.randrange(256) for _ in range(LAYOUT[key][1]))
            decoded = dict(decoder.decode(decoder.messages[key], data))
            expected = oracle(key, data)
            self.assertEqual(decoded.keys(), expected.keys())
            for name in expected:
                self.assertEqual(type(decoded[name]), type(expected[name]), (name, data.hex()))
                self.assertAlmostEqual(decoded[name], expected[name], places=9, msg=(name, data.hex()))

    def test_cantools_own_lookup_aliases_but_the_lab_does_not(self):
        import cantools
        database = cantools.database.load_string(SYNTHETIC_DBC, database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_frame_id(0x123).name, "SYNTH_STD")  # the 29-bit one is hidden
        decoder = canreplay.load_cantools_decoder(SYNTHETIC_DBC, canreplay.ReplayLimits())
        self.assertEqual(decoder.messages[(0x123, True)].name, "SYNTH_EXT")
        self.assertEqual(decoder.messages[(0x123, False)].name, "SYNTH_STD")

    def test_invalid_dbcs_fail_closed(self):
        overlapping = SYNTHETIC_DBC.replace('SG_ std_s8 : 16|8@1-', 'SG_ std_s8 : 8|8@1-')
        fd = SYNTHETIC_DBC.replace("BO_ 1792 SYNTH_EMPTY: 0 SYNTH", "BO_ 1792 SYNTH_FD: 64 SYNTH")
        too_long_std = SYNTHETIC_DBC.replace("BO_ 1792 SYNTH_EMPTY", "BO_ 4095 SYNTH_EMPTY")
        for text in (overlapping, fd, too_long_std, "not a dbc at all", SYNTHETIC_DBC.replace("BO_ 291", "BO_ x")):
            self.dbc.write_text(text, encoding="utf-8")
            code, stderr = self.run_real()
            self.assertEqual(code, 4, (text[:40], stderr))
            self.assertNotIn("SYNTH", stderr)  # parser text is not echoed
            self.assertFalse(self.out.exists())

    def test_duplicate_id_and_format_is_rejected_although_cantools_accepts_it(self):
        """cantools 44.1.0 strict mode accepts two 11-bit messages with id 0x123 and only logs a
        warning naming them; the lab must reject the DBC and must not echo the names."""
        import cantools
        duplicate = SYNTHETIC_DBC.replace("BO_ 1792 SYNTH_EMPTY: 0 SYNTH", "BO_ 291 SYNTH_DUP: 0 SYNTH")
        names = [m.name for m in cantools.database.load_string(duplicate, database_format="dbc", strict=True).messages]
        self.assertIn("SYNTH_DUP", names)  # accepted by cantools itself
        self.dbc.write_text(duplicate, encoding="utf-8")
        code, stderr = self.run_real()
        self.assertEqual(code, 4, stderr)
        self.assertNotIn("SYNTH", stderr)
        self.assertFalse(self.out.exists())

    def test_review_F7_duplicate_names_and_empty_dbc_are_rejected_silently(self):
        duplicate_name = SYNTHETIC_DBC.replace("SYNTH_EMPTY", "SYNTH_STD")
        empty = 'VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: SYNTH\n'
        for text in (duplicate_name, empty):
            self.dbc.write_text(text, encoding="utf-8")
            code, stderr = self.run_real()
            self.assertEqual(code, 4, stderr)
            self.assertNotIn("SYNTH_STD", stderr)

    def test_review_F8_values_beyond_double_range_are_decode_errors(self):
        huge = SYNTHETIC_DBC.replace("BO_ 1792 SYNTH_EMPTY: 0 SYNTH",
                                     'BO_ 1792 SYNTH_HUGE: 8 SYNTH\n SG_ huge : 0|64@1+ (1e300,0) [0|0] "" SYNTH')
        self.dbc.write_text(huge, encoding="utf-8")
        self.frames.write_bytes(self.frames.read_bytes().replace(b"0x700,0,\n", b"0x700,0,FFFFFFFFFFFFFFFF\n"))
        code, stderr = self.run_real()
        self.assertEqual(code, 0, stderr)
        last = json.loads(self.out.read_text().splitlines()[-1])
        self.assertEqual((last["status"], last["error"]), ("decode_error", "value_out_of_range"))

    def test_review_F9_dbc_bom_is_accepted_like_the_frame_bom(self):
        self.dbc.write_bytes(b"\xef\xbb\xbf" + self.dbc.read_bytes())
        code, stderr = self.run_real()
        self.assertEqual(code, 0, stderr)

    def test_fresh_process_imports_cantools_under_the_guard(self):
        """In a new interpreter the cantools import itself runs inside no_device_access()."""
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        result = subprocess.run([sys.executable, "-m", "pexislink", "lab", "can-replay", "--dbc", str(self.dbc),
                                 "--frames", str(self.frames), "--out", str(self.out), "--summary",
                                 str(self.summary)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.out.read_text().splitlines()), len(CLEAN_ROWS))


class NotRunReport(unittest.TestCase):
    def test_report_whether_the_real_decoder_ran(self):
        if not HAVE_CANTOOLS:
            print("\n[can-replay] real cantools integration: not_run (cantools not installed)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
