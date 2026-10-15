"""v0.4 Lab A: offline CAN replay. Standard-library only: the decoder here is the
SYNTHETIC stub (``canreplay_fixtures.StubDecoder``), so these tests check the
lab's own contract, not cantools. The real-decoder test is test_canreplay_cantools.py."""

import io
import json
import os
import socket
import sys
import tempfile
import tracemalloc
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from canreplay_fixtures import CLEAN_ROWS, EXPECTED, SYNTHETIC_DBC, StubDecoder, frames_csv, stub_factory, \
    write_inputs

from pexislink import canreplay, lab_cli
from pexislink.canreplay import ReplayError, parse_frame, parse_timestamp
from pexislink.cli import main as cli_main


class LabCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.inputs = self.root / "inputs"
        self.outdir = self.root / "outputs"
        self.outdir.mkdir()
        self.out = self.outdir / "records.jsonl"
        self.summary = self.outdir / "summary.json"

    def tearDown(self):
        self._tmp.cleanup()

    def run_lab(self, *extra, dbc=None, frames=None, out=None, summary=None, factory=stub_factory):
        stderr = io.StringIO()
        argv = ["can-replay", "--dbc", str(dbc or self.inputs / "synthetic.dbc"),
                "--frames", str(frames or self.inputs / "synthetic_frames.csv"),
                "--out", str(out or self.out), "--summary", str(summary or self.summary), *extra]
        with redirect_stderr(stderr):
            code = lab_cli.main(argv, decoder_factory=factory)
        return code, stderr.getvalue()

    def records(self):
        return [json.loads(line) for line in self.out.read_text().splitlines()]

    def assert_no_output(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(self.summary.exists())
        self.assertEqual([p.name for p in self.outdir.iterdir()], [])  # no temporary files left


class TimestampAndFrameContractTests(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(parse_timestamp("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(parse_timestamp("1970-01-01T01:00:00.5+01:00"), 500_000)
        self.assertEqual(parse_timestamp("2026-01-01T00:00:00.123456Z") % 1_000_000, 123_456)
        for bad in ("2026-01-01 00:00:00Z", "2026-01-01T00:00Z", "2026-01-01T00:00:00", "2026-02-30T00:00:00Z",
                    "2026-01-01T00:00:00.1234567Z", "2026-01-01T00:00:00+24:00", "1969-12-31T23:59:59Z",
                    "2200-01-01T00:00:00Z", "2026-01-01T00:00:00z", "٢026-01-01T00:00:00Z", "nan"):
            self.assertIsNone(parse_timestamp(bad), bad)

    def frame(self, ident="0x123", ext="0", data="00"):
        return parse_frame(2, f"2026-01-01T00:00:00Z,{ident},{ext},{data}")

    def assert_rejected(self, code, **kwargs):
        with self.assertRaises(ReplayError) as caught:
            self.frame(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.line, 2)

    def test_identifier_ranges_follow_the_frame_format(self):
        self.assertEqual(self.frame(ident="0x7FF").arbitration_id, 0x7FF)
        self.assertEqual(self.frame(ident="0x1FFFFFFF", ext="1").arbitration_id, 0x1FFFFFFF)
        self.assertTrue(self.frame(ident="0x0", ext="1").is_extended_id)
        self.assert_rejected("arbitration_id_out_of_range", ident="0x800")
        self.assert_rejected("arbitration_id_out_of_range", ident="0x20000000", ext="1")
        for ident in ("123", "0X123", "-0x1", "0x", "0x123456789", "0x12G", " 0x1"):
            self.assert_rejected("invalid_arbitration_id", ident=ident)
        for ext in ("true", "", "2", " 0"):
            self.assert_rejected("invalid_extended_flag", ext=ext)

    def test_payload_contract(self):
        self.assertEqual(self.frame(data="").data, b"")
        self.assertEqual(self.frame(data="0123456789abcdef").data, bytes.fromhex("0123456789abcdef"))
        self.assert_rejected("unsupported_frame", data="00" * 9)
        for data in ("0", "0G", "00 11", "0x00", "00" * 8 + "0", "-1"):
            self.assert_rejected("invalid_data_hex", data=data)

    def test_quotes_and_field_count(self):
        with self.assertRaises(ReplayError) as caught:
            parse_frame(3, '2026-01-01T00:00:00Z,"0x123",0,00')
        self.assertEqual(caught.exception.code, "csv_quoting")
        for line in ("", "2026-01-01T00:00:00Z,0x123,0", "2026-01-01T00:00:00Z,0x123,0,00,extra"):
            with self.assertRaises(ReplayError) as caught:
                parse_frame(3, line)
            self.assertEqual(caught.exception.code, "field_count")


class ReplayTests(LabCase):
    def test_every_frame_produces_exactly_one_record_in_input_order(self):
        write_inputs(self.inputs)
        code, stderr = self.run_lab()
        self.assertEqual(code, 0, stderr)
        records = self.records()
        self.assertEqual([r["line"] for r in records], list(range(2, 2 + len(CLEAN_ROWS))))
        self.assertEqual([r["status"] for r in records],
                         ["decoded", "decoded", "decoded", "unknown_message", "decode_error", "decoded"])

    def test_standard_and_extended_ids_with_the_same_number_do_not_alias(self):
        write_inputs(self.inputs)
        self.run_lab()
        std, ext = self.records()[:2]
        self.assertEqual((std["message"], std["arbitration_id"], std["is_extended_id"]),
                         ("SYNTH_STD", "0x123", False))
        self.assertEqual((ext["message"], ext["arbitration_id"], ext["is_extended_id"]),
                         ("SYNTH_EXT", "0x00000123", True))
        for record in (std, ext):
            key = (record["arbitration_id"] if not record["is_extended_id"] else "0x123", record["is_extended_id"])
            payload = next(p for (i, e, p) in EXPECTED if (i, e) == key)
            values = {s["name"]: s["value"] for s in record["signals"]}
            self.assertEqual(values, EXPECTED[(key[0], key[1], payload)])

    def test_units_come_only_from_the_dbc(self):
        write_inputs(self.inputs)
        self.run_lab()
        units = {s["name"]: s["unit"] for s in self.records()[0]["signals"]}
        self.assertEqual(units, {"std_u16": "degC", "std_s8": None, "std_s12": "V"})
        unknown = self.records()[3]
        self.assertNotIn("signals", unknown)
        self.assertNotIn("message", unknown)

    def test_length_mismatch_is_a_row_level_decode_error(self):
        write_inputs(self.inputs)
        self.run_lab()
        record = self.records()[4]
        self.assertEqual((record["status"], record["error"], record["dlc"], record["expected_dlc"]),
                         ("decode_error", "length_mismatch", 2, 8))

    def test_timestamp_order_is_recorded_not_repaired(self):
        rows = ["2026-01-01T00:00:01Z,0x456,0,01", "2026-01-01T00:00:01Z,0x456,0,01",
                "2026-01-01T00:00:00Z,0x456,0,02", "2026-01-01T00:00:02Z,0x456,0,03"]
        write_inputs(self.inputs, rows)
        self.run_lab()
        self.assertEqual([r["order"] for r in self.records()], ["first", "equal", "backwards", "increasing"])
        order = json.loads(self.summary.read_text())["frames"]["timestamp_order"]
        self.assertEqual(order, {"backwards": 1, "equal": 1, "exact_duplicate_of_previous": 1, "first": 1,
                                 "increasing": 1})

    def test_summary_is_the_commit_marker(self):
        write_inputs(self.inputs)
        self.run_lab()
        summary = json.loads(self.summary.read_text())
        import hashlib
        self.assertEqual(summary["records"]["sha256"], hashlib.sha256(self.out.read_bytes()).hexdigest())
        self.assertEqual(summary["records"]["count"], len(CLEAN_ROWS))
        self.assertEqual(summary["inputs"]["frames"]["sha256"],
                         hashlib.sha256((self.inputs / "synthetic_frames.csv").read_bytes()).hexdigest())
        self.assertEqual(summary["frames"]["status_counts"], {"decoded": 4, "unknown_message": 1, "decode_error": 1})
        self.assertEqual(summary["schema"], "pexislink.can-replay.v1")
        self.assertEqual([m["name"] for m in summary["dbc_schema"]], ["SYNTH_STD", "SYNTH_EXT", "SYNTH_EMPTY"])  # by (id, extended)

    def test_outputs_are_deterministic_and_carry_no_payload_or_path(self):
        write_inputs(self.inputs)
        self.run_lab()
        first = (self.out.read_bytes(), self.summary.read_bytes())
        self.run_lab()
        self.assertEqual(first, (self.out.read_bytes(), self.summary.read_bytes()))
        text = first[0].decode() + first[1].decode()
        for payload in ("1027FE340F000000", "7B008000", "1027"):
            self.assertNotIn(payload, text)
        self.assertNotIn(str(self.root), text)

    def test_bom_crlf_and_missing_final_newline_are_accepted(self):
        data = b"\xef\xbb\xbf" + frames_csv(CLEAN_ROWS[:2], newline="\r\n", final_newline=False)
        write_inputs(self.inputs, frames=data)
        code, _ = self.run_lab()
        self.assertEqual(code, 0)
        inputs = json.loads(self.summary.read_text())["inputs"]["frames"]
        self.assertEqual((inputs["utf8_bom"], inputs["final_newline"]), (True, False))

    def test_header_only_file_produces_no_records(self):
        write_inputs(self.inputs, [])
        self.assertEqual(self.run_lab()[0], 0)
        self.assertEqual(self.out.read_bytes(), b"")

    def test_entry_point_dispatches_the_lab(self):
        write_inputs(self.inputs)
        with mock.patch.object(lab_cli, "load_cantools_decoder", stub_factory), \
                mock.patch.object(lab_cli.main, "__defaults__", (stub_factory,)), redirect_stderr(io.StringIO()):
            code = cli_main(["lab", "can-replay", "--dbc", str(self.inputs / "synthetic.dbc"), "--frames",
                             str(self.inputs / "synthetic_frames.csv"), "--out", str(self.out),
                             "--summary", str(self.summary)])
        self.assertEqual(code, 0)


class DecoderOutputTests(LabCase):
    def decoding(self, value):
        class Odd(StubDecoder):
            def decode(self, message, data):
                if isinstance(value, BaseException):
                    raise value
                return value
        write_inputs(self.inputs, CLEAN_ROWS[:1])
        code, stderr = self.run_lab(factory=lambda text, limits: Odd())
        self.assertEqual(code, 0, stderr)
        return self.records()[0], stderr

    def test_nonfinite_values_are_decode_errors(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(self.decoding({"std_u16": bad})[0]["error"], "nonfinite_value")

    def test_unsupported_value_types_are_decode_errors(self):
        for bad in (True, "9", b"\x00", None, [1]):
            self.assertEqual(self.decoding({"std_u16": bad})[0]["error"], "unsupported_value_type")

    def test_signals_not_in_the_dbc_are_rejected(self):
        self.assertEqual(self.decoding({"invented": 1})[0]["error"], "unexpected_signal")

    def test_decoder_exception_text_never_leaks(self):
        record, stderr = self.decoding(ValueError("SECRET payload 1027FE"))
        self.assertEqual((record["status"], record["error"]), ("decode_error", "decode_failed"))
        self.assertNotIn("SECRET", self.out.read_text() + self.summary.read_text() + stderr)


class GateFailureTests(LabCase):
    def assert_gate(self, expected_code, error_code, *extra, **kwargs):
        code, stderr = self.run_lab(*extra, **kwargs)
        self.assertEqual(code, expected_code, stderr)
        self.assertIn(f"error: {error_code}", stderr)
        self.assertNotIn(str(self.root), stderr)
        self.assert_no_output()

    def test_corrupt_frame_files_exit_5(self):
        cases = [
            (b"timestamp,arbitration_id,data_hex,is_extended_id\n", "invalid_header"),
            (b"", "invalid_header"),
            (frames_csv(CLEAN_ROWS[:1]).replace(b"0x123", b"\"0x123\""), "csv_quoting"),
            (frames_csv([]) + b"2026-01-01T00:00:00Z,0x123,0,\xff\n", "invalid_utf8"),
            (frames_csv([]) + b"2026-01-01T00:00:00Z,0x123,0,00\r2026\n", "invalid_line"),
            (frames_csv(["2026-01-01T00:00:00Z,0x123,0,0G"]), "invalid_data_hex"),
            (frames_csv(["2026-01-01T00:00:00Z,0x123,0,00", ""]), "field_count"),
        ]
        for data, error in cases:
            write_inputs(self.inputs, frames=data)
            self.assert_gate(5, error)

    def test_unsupported_frame_exits_6(self):
        write_inputs(self.inputs, ["2026-01-01T00:00:00Z,0x123,0," + "00" * 64])
        self.assert_gate(6, "unsupported_frame")

    def test_missing_and_non_regular_inputs_exit_7(self):
        write_inputs(self.inputs)
        self.assert_gate(7, "input_unreadable", frames=self.root / "nowhere.csv")
        self.assert_gate(7, "input_unreadable", dbc=self.inputs)

    def test_symlinked_input_exits_8(self):
        write_inputs(self.inputs)
        link = self.root / "link.csv"
        link.symlink_to(self.inputs / "synthetic_frames.csv")
        self.assert_gate(8, "input_symlink", frames=link)

    def test_size_limits_exit_9(self):
        write_inputs(self.inputs, ["2026-01-01T00:00:00Z,0x123,0,00"] * 3)
        self.assert_gate(9, "input_too_large", "--max-rows", "2")
        write_inputs(self.inputs, ["2026-01-01T00:00:00Z,0x123,0,00" + "0" * 300])
        self.assert_gate(9, "line_too_long")
        write_inputs(self.inputs, dbc=SYNTHETIC_DBC + " " * 2048)
        self.assert_gate(9, "input_too_large", "--max-dbc-kb", "1")
        write_inputs(self.inputs, frames=frames_csv(["2026-01-01T00:00:00Z,0x456,0,00"] * 40_000))  # > 1 MiB
        self.assert_gate(9, "input_too_large", "--max-frames-mb", "1")

    def test_input_growing_or_rewritten_during_the_read_exits_10(self):
        for change in ("append", "same_size"):
            write_inputs(self.inputs, CLEAN_ROWS)
            path = self.inputs / "synthetic_frames.csv"

            class Mutating(StubDecoder):
                done = False

                def decode(self, message, data):
                    if not Mutating.done:
                        Mutating.done = True
                        if change == "append":
                            with open(path, "ab") as handle:
                                handle.write(b"2026-01-01T00:00:09Z,0x456,0,00\n")
                        else:
                            content = path.read_bytes()
                            path.write_bytes(content.replace(b"0x456", b"0x457"))
                            stamp = os.stat(path).st_mtime_ns + 5_000_000_000
                            os.utime(path, ns=(stamp, stamp))
                    return super().decode(message, data)

            code, stderr = self.run_lab(factory=lambda text, limits: Mutating())
            self.assertEqual(code, 10, (change, stderr))
            self.assert_no_output()

    def test_non_utf8_dbc_exits_4(self):
        write_inputs(self.inputs)
        (self.inputs / "synthetic.dbc").write_bytes(b"VERSION \"\xff\"\n")
        self.assert_gate(4, "invalid_dbc")

    def test_missing_optional_decoder_exits_3_with_install_hint(self):
        write_inputs(self.inputs)
        with mock.patch.dict(sys.modules, {"cantools": None, "cantools.database": None}):
            code, stderr = self.run_lab(factory=canreplay.load_cantools_decoder)
        self.assertEqual(code, 3)
        self.assertIn("pip install 'pexislink[can]'", stderr)
        self.assert_no_output()


class BoundedInputTests(unittest.TestCase):
    def test_the_byte_cap_holds_while_streaming(self):
        """A file that grows after the size check is cut off at the cap, not read to its end."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.csv"
            path.write_bytes(b"x" * 10)
            with canreplay.open_input(path, 16, "frame") as source:
                with open(path, "ab") as grow:
                    grow.write(b"y" * 1000)
                with self.assertRaises(ReplayError) as caught:
                    source.read()
                self.assertEqual(caught.exception.code, "input_changed")
                self.assertLessEqual(source.bytes_read, 16 + 1010)
                with self.assertRaises(ReplayError):
                    source.finish()


class OutputSafetyTests(LabCase):
    def test_outputs_may_not_alias_inputs_or_each_other(self):
        write_inputs(self.inputs)
        frames, dbc = self.inputs / "synthetic_frames.csv", self.inputs / "synthetic.dbc"
        before = frames.read_bytes(), dbc.read_bytes()
        for kwargs in ({"out": frames}, {"summary": dbc}, {"summary": self.out},
                       {"out": self.inputs / "." / "synthetic_frames.csv"}):
            code, stderr = self.run_lab(**kwargs)
            self.assertEqual(code, 2, (kwargs, stderr))
        hard = self.root / "hardlink.csv"
        os.link(frames, hard)
        self.assertEqual(self.run_lab(out=hard)[0], 2)
        self.assertEqual((frames.read_bytes(), dbc.read_bytes()), before)

    def test_outputs_must_be_regular_files(self):
        write_inputs(self.inputs)
        target = self.root / "victim.txt"
        target.write_text("keep")
        link = self.outdir / "link.jsonl"
        link.symlink_to(target)
        self.assertEqual(self.run_lab(out=link)[0], 2)
        self.assertEqual(self.run_lab(out=self.outdir)[0], 2)
        self.assertEqual(target.read_text(), "keep")

    def test_unwritable_destination_exits_1(self):
        write_inputs(self.inputs)
        code, stderr = self.run_lab(out=self.root / "missing" / "r.jsonl")
        self.assertEqual(code, 1)
        self.assertIn("output_write_error", stderr)
        self.assertNotIn(str(self.root), stderr)

    def test_a_failed_run_leaves_previous_outputs_untouched(self):
        write_inputs(self.inputs)
        self.out.write_text("previous records\n")
        self.summary.write_text("previous summary\n")
        write_inputs(self.inputs, ["bad row"])
        self.assertEqual(self.run_lab()[0], 5)
        self.assertEqual((self.out.read_text(), self.summary.read_text()), ("previous records\n", "previous summary\n"))
        self.assertEqual(sorted(p.name for p in self.outdir.iterdir()), ["records.jsonl", "summary.json"])

    def test_a_stale_summary_never_pairs_with_new_records(self):
        """If the run stops between the two renames, no old summary remains next to the new records."""
        write_inputs(self.inputs)
        self.out.write_text("old records\n")
        self.summary.write_text("old summary\n")
        real_commit = lab_cli._Pending.commit

        def commit(pending):
            if pending.target == self.summary:
                raise OSError(28, "No space left on device")
            real_commit(pending)

        with mock.patch.object(lab_cli._Pending, "commit", commit):
            code, stderr = self.run_lab()
        self.assertEqual(code, 1, stderr)
        self.assertFalse(self.summary.exists())  # the interrupted state the docs describe
        self.assertNotEqual(self.out.read_text(), "old records\n")
        self.assertEqual(sorted(p.name for p in self.outdir.iterdir()), ["records.jsonl"])

    def test_interrupt_leaves_no_temporary_files(self):
        write_inputs(self.inputs)

        class Interrupted(StubDecoder):
            def decode(self, message, data):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt), redirect_stderr(io.StringIO()):
            lab_cli.main(["can-replay", "--dbc", str(self.inputs / "synthetic.dbc"), "--frames",
                          str(self.inputs / "synthetic_frames.csv"), "--out", str(self.out),
                          "--summary", str(self.summary)], decoder_factory=lambda text, limits: Interrupted())
        self.assert_no_output()

    def test_usage_errors_exit_2(self):
        write_inputs(self.inputs)
        self.assertEqual(self.run_lab("--max-rows", "0")[0], 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            lab_cli.main(["can-replay", "--dbc", "x"])
        self.assertEqual(caught.exception.code, 2)


class SafetyGuardTests(LabCase):
    def attempt(self, action):
        class Rogue(StubDecoder):
            def decode(self, message, data):
                try:
                    action()
                except Exception:
                    pass  # a library that swallows the refusal still fails the run
                return super().decode(message, data)

        write_inputs(self.inputs, CLEAN_ROWS[:1])
        code, stderr = self.run_lab(factory=lambda text, limits: Rogue())
        self.assertEqual(code, 11, stderr)
        self.assertIn("safety_guard", stderr)
        self.assert_no_output()

    def test_socket_creation_is_refused(self):
        self.attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))

    def test_process_creation_is_refused(self):
        import subprocess
        self.attempt(lambda: subprocess.run(["true"], check=False))

    def test_device_open_is_refused(self):
        self.attempt(lambda: open("/dev/null", "rb"))

    def test_native_library_loading_is_refused(self):
        import ctypes
        self.attempt(lambda: ctypes.CDLL(None))

    def test_guard_is_inactive_outside_a_replay(self):
        with canreplay.no_device_access():
            pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            pass  # other code in the same process is unaffected

    def test_guard_failure_in_the_factory_also_fails_closed(self):
        def factory(text, limits):
            try:
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            except Exception:
                pass
            return StubDecoder()
        write_inputs(self.inputs)
        self.assertEqual(self.run_lab(factory=factory)[0], 11)
        self.assert_no_output()


class StreamingTests(LabCase):
    def peak(self, rows):
        frames = frames_csv([f"2026-01-01T00:00:{i % 60:02d}.{i % 1000:03d}Z,0x{0x100 + i % 512:03X},0,0011"
                             for i in range(rows)])
        directory = self.root / f"in{rows}"
        write_inputs(directory, frames=frames)
        tracemalloc.start()
        try:
            code, stderr = self.run_lab("--max-frames-mb", "64", dbc=directory / "synthetic.dbc",
                                        frames=directory / "synthetic_frames.csv")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(code, 0, stderr)
        return peak

    def test_memory_does_not_grow_with_rows(self):
        small, large = self.peak(5_000), self.peak(50_000)
        self.assertLess(large, small * 1.5 + 256 * 1024, (small, large))


class ReviewRegressionTests(LabCase):
    """Independent adversarial review of aaac4dd. Each test failed on aaac4dd."""

    def rogue(self, action, swallow=True):
        class Rogue(StubDecoder):
            def decode(self, message, data):
                if swallow:
                    try:
                        action()
                    except Exception:
                        pass
                else:
                    action()
                return super().decode(message, data)
        return lambda text, limits: Rogue()

    def assert_guard(self, factory):
        write_inputs(self.inputs, CLEAN_ROWS[:1])
        code, stderr = self.run_lab(factory=factory)
        self.assertEqual(code, 11, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_no_output()

    def test_F1_an_uncaught_refusal_exits_11_without_traceback(self):
        def factory(text, limits):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # not caught by anyone
            return StubDecoder()
        self.assert_guard(factory)

    @unittest.skipUnless(os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK), "no writable /dev/shm")
    def test_F1_regular_files_under_dev_shm_are_inputs_not_devices(self):
        shm = Path(tempfile.mkdtemp(dir="/dev/shm"))
        try:
            dbc, frames = write_inputs(shm)
            code, stderr = self.run_lab(dbc=dbc, frames=frames)
            self.assertEqual(code, 0, stderr)
        finally:
            for child in shm.iterdir():
                child.unlink()
            shm.rmdir()

    def test_F2_name_resolution_is_refused(self):
        self.assert_guard(self.rogue(lambda: socket.getaddrinfo("localhost", 80)))
        self.assert_guard(self.rogue(lambda: socket.gethostbyname("localhost")))

    def test_F2_device_paths_spelled_differently_are_refused(self):
        self.assert_guard(self.rogue(lambda: open("//dev/null", "rb")))
        self.assert_guard(self.rogue(lambda: open("/./dev/null", "rb")))

        def via_chdir():
            cwd = os.getcwd()
            os.chdir("/dev")
            try:
                open("null", "rb")
            finally:
                os.chdir(cwd)
        self.assert_guard(self.rogue(via_chdir))

    def test_F2_a_thread_left_running_fails_the_run(self):
        import threading
        stop = threading.Event()

        def start():
            threading.Thread(target=stop.wait, daemon=True).start()
        try:
            self.assert_guard(self.rogue(start))
        finally:
            stop.set()

    def test_F2_more_process_entry_points_are_refused(self):
        self.assert_guard(self.rogue(lambda: os.system("true")))
        self.assert_guard(self.rogue(lambda: os.posix_spawn("/bin/true", ["true"], {})))

    def test_F3_a_dbc_with_too_many_signals_is_rejected_before_parsing(self):
        lines = [SYNTHETIC_DBC, "BO_ 1793 SYNTH_WIDE: 8 SYNTH"]
        lines += [f" SG_ wide{i} : 0|1@1+ (1,0) [0|1] \"\" SYNTH" for i in range(600)]
        write_inputs(self.inputs, dbc="\n".join(lines) + "\n")
        called = []
        code, stderr = self.run_lab(factory=lambda text, limits: called.append(1) or
                                    canreplay.load_cantools_decoder(text, limits))
        self.assertEqual(code, 4, stderr)
        self.assertIn("per-message limit", stderr)
        write_inputs(self.inputs)
        self.assertEqual(self.run_lab("--max-signals", "3",
                                      factory=canreplay.load_cantools_decoder)[0], 4)

    def test_F4_interrupt_during_commit_leaves_no_temporary_file(self):
        write_inputs(self.inputs)
        real_commit = lab_cli._Pending.commit

        def commit(pending):
            if pending.target == self.summary:
                raise KeyboardInterrupt
            real_commit(pending)

        with mock.patch.object(lab_cli._Pending, "commit", commit), self.assertRaises(KeyboardInterrupt):
            self.run_lab()
        self.assertEqual(sorted(p.name for p in self.outdir.iterdir()), ["records.jsonl"])

    def test_F5_capped_unknown_id_count_is_flagged(self):
        write_inputs(self.inputs, [f"2026-01-01T00:00:00Z,0x{0x400 + i:03X},0,00" for i in range(10)])
        with mock.patch.object(canreplay, "_UNKNOWN_ID_CAP", 3):
            self.run_lab()
        frames = json.loads(self.summary.read_text())["frames"]
        self.assertEqual((frames["distinct_unknown_ids"], frames["distinct_unknown_ids_capped"]), (3, True))

    def test_F6_line_limit_counts_content_bytes_only(self):
        row = "2026-01-01T00:00:00.000000+01:00,0x00000123,1,7B008000"  # 54 bytes, longer than the header
        for final in (True, False):
            for newline in ("\n", "\r\n"):
                write_inputs(self.inputs, frames=frames_csv([row], newline=newline, final_newline=final))
                self.assertEqual(self.run_lab("--max-line-bytes", str(len(row)))[0], 0)
                self.assertEqual(self.run_lab("--max-line-bytes", str(len(row) - 1))[0], 9)
        write_inputs(self.inputs, frames=b"\xef\xbb\xbf" + frames_csv(["2026-01-01T00:00:00Z,0x123,0,00"]))
        self.assertEqual(self.run_lab("--max-line-bytes", "48")[0], 0)  # header is 48 content bytes

    def test_F9_long_output_names_and_existing_permissions(self):
        write_inputs(self.inputs)
        long_out = self.outdir / ("r" * 200 + ".jsonl")
        self.assertEqual(self.run_lab(out=long_out)[0], 0)
        self.summary.write_text("old")
        os.chmod(self.summary, 0o600)
        self.assertEqual(self.run_lab()[0], 0)
        self.assertEqual(os.stat(self.summary).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
