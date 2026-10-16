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

    def test_grown_line_never_reads_more_than_the_remaining_file_budget(self):
        """A huge line limit cannot allocate the size of an appended long line."""
        for operation in ("read", "readline"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "grow.csv"
                path.write_bytes(b"initial")
                with canreplay.open_input(path, 64, "frame") as source:
                    with open(path, "ab") as writer:
                        writer.write(b"x" * (64 * 1024))
                    with self.assertRaises(ReplayError) as error:
                        if operation == "readline":
                            source.readline(1 << 40)
                        else:
                            source.read()
                    self.assertEqual(error.exception.code, "input_changed")
                    self.assertEqual(source.bytes_read, 65)


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

    def test_old_summary_is_synced_before_records_commit_even_in_another_directory(self):
        write_inputs(self.inputs)
        other = self.root / "separate-summary"
        other.mkdir()
        summary = other / "summary.json"
        summary.write_text("stale summary")
        self.out.write_text("stale records")
        real_commit = lab_cli._Pending.commit
        real_sync = lab_cli._fsync_directory
        events = []

        def commit(pending):
            events.append(("commit", pending.target))
            return real_commit(pending)

        def sync(directory):  # every directory fsync is required since the 7242970 review
            events.append(("sync", Path(directory)))
            return real_sync(directory)

        with mock.patch.object(lab_cli._Pending, "commit", commit), \
             mock.patch.object(lab_cli, "_fsync_directory", sync):
            code, stderr = self.run_lab(summary=summary)
        self.assertEqual(code, 0, stderr)
        self.assertTrue(summary.is_file())
        summary_dir = other.resolve()
        # the stale-summary unlink is synced once before the records commit, then again after the
        # new summary's rename
        self.assertEqual(events.count(("sync", summary_dir)), 2, events)
        self.assertLess(events.index(("sync", summary_dir)), events.index(("commit", self.out.resolve())))

    def test_old_summary_fsync_failure_prevents_records_commit(self):
        write_inputs(self.inputs)
        other = self.root / "separate-summary"
        other.mkdir()
        summary = other / "summary.json"
        summary.write_text("old summary")
        self.out.write_text("old records")

        def cannot_sync(directory):
            raise OSError(5, "synthetic fsync failure")

        with mock.patch.object(lab_cli, "_fsync_directory", cannot_sync):
            code, stderr = self.run_lab(summary=summary)
        self.assertEqual(code, 1, stderr)
        self.assertIn("output_write_error", stderr)
        self.assertEqual(self.out.read_text(), "old records")
        self.assertFalse(summary.exists())
        self.assertEqual(sorted(p.name for p in other.iterdir()), [])

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

    def test_low_level_and_joinable_thread_start_are_refused(self):
        # Python 3.11/3.12 use _thread.start_new_thread; Python 3.13+
        # may use _thread.start_joinable_thread for threading.Thread.start().
        import _thread
        import threading

        self.attempt(lambda: _thread.start_new_thread(lambda: None, ()))
        self.attempt(lambda: threading.Thread(target=lambda: None, daemon=True).start())

    def test_raw_thread_wrappers_are_restored_after_normal_and_failed_guard(self):
        import _thread
        import threading

        original_raw = _thread.start_new_thread
        original_start = threading.Thread.start
        original_alias = getattr(threading, "_start_new_thread", None)

        with canreplay.no_device_access():
            self.assertIsNot(_thread.start_new_thread, original_raw)
            self.assertIsNot(threading.Thread.start, original_start)
        self.assertIs(_thread.start_new_thread, original_raw)
        self.assertIs(threading.Thread.start, original_start)
        self.assertIs(getattr(threading, "_start_new_thread", None), original_alias)

        with self.assertRaises(ReplayError):
            with canreplay.no_device_access():
                try:
                    _thread.start_new_thread(lambda: None, ())
                except canreplay.SafetyViolation:
                    pass
        self.assertIs(_thread.start_new_thread, original_raw)
        self.assertIs(threading.Thread.start, original_start)
        self.assertIs(getattr(threading, "_start_new_thread", None), original_alias)

    def test_guard_is_inactive_outside_a_replay(self):
        with canreplay.no_device_access():
            pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            pass  # other code in the same process is unaffected

    def test_nested_guard_cannot_disable_outer_guard(self):
        # Regression: a nested context used to clear the process-global active
        # flag, allowing a subsequent socket attempt to bypass the audit hook.
        still_blocked = []
        with self.assertRaises(ReplayError) as outer:
            with canreplay.no_device_access():
                with self.assertRaises(ReplayError) as inner:
                    with canreplay.no_device_access():
                        pass
                self.assertEqual(inner.exception.code, "safety_guard")
                # Record proof outside the outer context: its own safety
                # error must not mask a failed socket-blocking assertion.
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
                        still_blocked.append(False)
                except canreplay.SafetyViolation:
                    still_blocked.append(True)
        self.assertEqual(still_blocked, [True])
        self.assertEqual(outer.exception.code, "safety_guard")
        self.assertFalse(canreplay._guard["active"])

    def test_concurrent_guard_entry_fails_closed_without_disabling_owner(self):
        import threading

        ready, begin, done = threading.Event(), threading.Event(), threading.Event()
        observed, still_blocked = [], []

        def competing_replay():
            ready.set()
            if begin.wait(timeout=5):
                try:
                    with canreplay.no_device_access():
                        observed.append("entered")
                except ReplayError as error:
                    observed.append(error.code)
            done.set()

        worker = threading.Thread(target=competing_replay)
        worker.start()
        try:
            self.assertTrue(ready.wait(timeout=5))
            with self.assertRaises(ReplayError) as outer:
                with canreplay.no_device_access():
                    begin.set()
                    self.assertTrue(done.wait(timeout=5))
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
                            still_blocked.append(False)
                    except canreplay.SafetyViolation:
                        still_blocked.append(True)
            self.assertEqual(still_blocked, [True])
            self.assertEqual(outer.exception.code, "safety_guard")
            self.assertEqual(observed, ["safety_guard"])
            self.assertFalse(canreplay._guard["active"])
        finally:
            begin.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_guard_activation_race_cannot_erase_concurrent_violation(self):
        """Reproduce Codex P1: park owner before it finishes initialization.

        A competing replay must not be able to trigger a refusal and create a
        socket in the interval before the owner marks the guard active.
        Socket *creation* only; there is never a network connection.
        """
        import threading

        original_enumerate = threading.enumerate
        paused = threading.Event()
        resume = threading.Event()
        attempting = threading.Event()
        completed = threading.Event()
        owner_identity = [None]
        contender_results = []
        owner_results = []

        def pause_during_setup():
            if threading.get_ident() == owner_identity[0] and not paused.is_set():
                paused.set()
                if not resume.wait(timeout=10):
                    raise AssertionError("guard setup was not resumed")
            return original_enumerate()

        def owner():
            owner_identity[0] = threading.get_ident()
            try:
                with canreplay.no_device_access():
                    if not completed.wait(timeout=10):
                        raise AssertionError("contender did not finish")
            except ReplayError as failure:
                owner_results.append(failure.code)
            except BaseException as failure:
                owner_results.append(type(failure).__name__)

        def contender():
            if not paused.wait(timeout=10):
                contender_results.append("owner_not_paused")
                completed.set()
                return
            attempting.set()
            try:
                with canreplay.no_device_access():
                    contender_results.append("entered")
            except ReplayError as failure:
                contender_results.append(failure.code)
            # A socket constructor is observable and safe; no connect/send.
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
                    contender_results.append("socket_allowed")
            except canreplay.SafetyViolation:
                contender_results.append("socket_blocked")
            finally:
                completed.set()

        other = threading.Thread(target=contender)
        holder = threading.Thread(target=owner)
        with mock.patch.object(threading, "enumerate", pause_during_setup):
            other.start()
            holder.start()
            try:
                self.assertTrue(paused.wait(timeout=10))
                self.assertTrue(attempting.wait(timeout=10))
            finally:
                resume.set()
            other.join(timeout=10)
            holder.join(timeout=10)
        self.assertFalse(other.is_alive())
        self.assertFalse(holder.is_alive())
        self.assertEqual(contender_results, ["safety_guard", "socket_blocked"])
        self.assertEqual(owner_results, ["safety_guard"])
        self.assertFalse(canreplay._guard["active"])

    def test_inflight_device_audit_finishes_before_guard_finalization(self):
        """An audit callback paused in device classification cannot outlive the guard.

        The worker and finisher are both pre-existing threads. Only /dev/null
        is opened, and the callback blocks its open before the syscall.
        """
        import threading

        started = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        request_exit = threading.Event()
        exiting = threading.Event()
        audit_done = threading.Event()
        final_done = threading.Event()
        worker_result, final_result = [], []
        original_is_device = canreplay._is_device

        def paused_classification(path):
            if path == "/dev/null":
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError("device classifier was not resumed")
                return True
            return original_is_device(path)

        def worker():
            if started.wait(timeout=10):
                try:
                    with open("/dev/null", "rb"):
                        worker_result.append("opened")
                except canreplay.SafetyViolation:
                    worker_result.append("blocked")
            audit_done.set()

        def closer():
            if request_exit.wait(timeout=10):
                exiting.set()
                try:
                    guard.__exit__(None, None, None)
                    final_result.append("succeeded")
                except ReplayError as error:
                    final_result.append(error.code)
            final_done.set()

        reader = threading.Thread(target=worker)
        finisher = threading.Thread(target=closer)
        reader.start()
        finisher.start()
        with mock.patch.object(canreplay, "_is_device", side_effect=paused_classification):
            guard = canreplay.no_device_access()
            guard.__enter__()
            try:
                started.set()
                self.assertTrue(entered.wait(timeout=10), "audit callback did not begin")
                request_exit.set()
                self.assertTrue(exiting.wait(timeout=10))
                self.assertFalse(final_done.wait(timeout=0.05), "guard exited before the in-flight audit")
            finally:
                release.set()
            self.assertTrue(audit_done.wait(timeout=10))
            self.assertTrue(final_done.wait(timeout=10))
        reader.join(timeout=10)
        finisher.join(timeout=10)
        self.assertFalse(reader.is_alive())
        self.assertFalse(finisher.is_alive())
        self.assertEqual(worker_result, ["blocked"])
        self.assertEqual(final_result, ["safety_guard"])
        self.assertFalse(canreplay._guard["active"])

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


class CodexReviewTests(LabCase):
    """Automated review of PR #10 (3a0d6fb). Each test failed on 3a0d6fb."""

    def test_guard_checks_run_even_when_another_error_propagates(self):
        import threading
        stop = threading.Event()

        def thread_then_error(text, limits):
            threading.Thread(target=stop.wait, daemon=True).start()
            raise ReplayError("invalid_dbc", "pretend parse failure")

        def refusal_turned_into_error(text, limits):
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except Exception:
                raise ReplayError("invalid_dbc", "a parser that wraps every exception") from None
            return StubDecoder()

        try:
            for factory in (thread_then_error, refusal_turned_into_error):
                write_inputs(self.inputs)
                code, stderr = self.run_lab(factory=factory)
                self.assertEqual(code, 11, stderr)
                self.assertIn("safety_guard", stderr)
                self.assert_no_output()
        finally:
            stop.set()

    def test_other_errors_still_propagate_unchanged(self):
        write_inputs(self.inputs)

        def failing(text, limits):
            raise ReplayError("invalid_dbc", "pretend parse failure")
        self.assertEqual(self.run_lab(factory=failing)[0], 4)

    def test_absurd_limits_are_usage_errors(self):
        write_inputs(self.inputs)
        for flag in ("--max-dbc-kb", "--max-frames-mb", "--max-line-bytes", "--max-rows"):
            code, stderr = self.run_lab(flag, str(10 ** 16))
            self.assertEqual(code, 2, (flag, stderr))
            self.assertNotIn("Traceback", stderr)


@unittest.skipUnless(os.path.isdir("/proc/self/fd"), "not_run: the descriptor and kernel-path checks need Linux /proc")
class CodexReview7242970Tests(LabCase):
    """Codex review of PR #10 at 7242970 (5 P1, 3 P2). Each test failed on 7242970."""

    # P1: raw-thread wrapper restored before deactivation -------------------------------------
    def test_native_thread_entry_points_are_never_restored_while_the_guard_is_active(self):
        import _thread
        import threading
        native_raw, native_start = _thread.start_new_thread, threading.Thread.start
        observed = []

        class Spy(dict):
            def __setitem__(self, key, value):
                if key == "active" and value is False:  # the deactivation instant
                    observed.append((_thread.start_new_thread is native_raw,
                                     threading.Thread.start is native_start))
                super().__setitem__(key, value)

        with mock.patch.object(canreplay, "_guard", Spy(active=False, violations=0)):
            with canreplay.no_device_access():
                pass
        self.assertEqual(observed, [(False, False)], "a native entry point was reachable while active")
        self.assertIs(_thread.start_new_thread, native_raw)  # still restored afterwards
        self.assertIs(threading.Thread.start, native_start)

    # P1: a refused audit hook must fail closed ----------------------------------------------
    def test_refused_audit_hook_fails_closed(self):
        import subprocess
        write_inputs(self.inputs, CLEAN_ROWS[:1])
        tests_dir = Path(__file__).resolve().parent
        code = (
            "import sys\n"
            "def veto(event, args):\n"
            "    if event == 'sys.addaudithook':\n"
            "        raise RuntimeError('embedding policy refuses further hooks')\n"
            "sys.addaudithook(veto)\n"
            f"sys.path[:0] = [{str(tests_dir)!r}, {str(Path(canreplay.__file__).resolve().parents[1])!r}]\n"
            "from canreplay_fixtures import stub_factory\n"
            "from pexislink import lab_cli\n"
            f"sys.exit(lab_cli.main(['can-replay', '--dbc', {str(self.inputs / 'synthetic.dbc')!r}, '--frames', "
            f"{str(self.inputs / 'synthetic_frames.csv')!r}, '--out', {str(self.out)!r}, '--summary', "
            f"{str(self.summary)!r}], decoder_factory=stub_factory))\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 11, result.stderr)
        self.assertIn("audit hook was refused", result.stderr)
        self.assert_no_output()

    # P1: kernel control files are regular files ------------------------------------------------
    def test_kernel_control_and_information_files_are_refused(self):
        for path in ("/proc/sysrq-trigger", "/proc/self/status", "/sys/class/net/eth0/mtu",
                     "/sys/kernel/does-not-exist", "/proc"):
            self.assertTrue(canreplay._is_device(path), path)
        regular = self.root / "regular.txt"
        regular.write_text("x")
        self.assertFalse(canreplay._is_device(str(regular)))
        # end to end, including a relative spelling after chdir
        SafetyGuardTests.attempt(self, lambda: open("/proc/self/status", "rb").close())
        cwd = os.getcwd()
        try:
            SafetyGuardTests.attempt(self, lambda: (os.chdir("/proc"), open("self/status", "rb").close()))
        finally:
            os.chdir(cwd)

    # P1: total generated bytes are capped before each write ------------------------------------
    def test_output_budget_is_enforced_before_each_write(self):
        target = self.outdir / "budget.jsonl"
        pending = lab_cli._Pending(target, 64)
        try:
            pending.write_record({"k": "v"})  # 10 bytes: fits
            with self.assertRaises(ReplayError) as caught:
                pending.write_record({"k": "x" * 100})
            self.assertEqual(caught.exception.code, "output_too_large")
            pending.handle.flush()
            self.assertEqual(pending.temporary.stat().st_size, pending.bytes)  # nothing past the budget
            self.assertLessEqual(pending.bytes, 64)
            with self.assertRaises(ReplayError):
                pending.write_text("y" * 60)  # the summary path uses the same budget
        finally:
            pending.discard()

    def test_output_budget_exit_code_and_no_commit(self):
        row = "2026-01-01T00:00:00Z,0x123,0,1027FE340F000000"
        write_inputs(self.inputs, [row] * 8000)  # ~2.5 MB of records
        code, stderr = self.run_lab("--max-output-mb", "1")
        self.assertEqual(code, 12, stderr)
        self.assertIn("output_too_large", stderr)
        self.assert_no_output()
        self.assertEqual(self.run_lab("--max-output-mb", "64")[0], 0)

    # P1: names that one case-insensitive or normalizing filesystem maps to one entry -----------
    def test_case_or_normalization_aliases_are_rejected(self):
        write_inputs(self.inputs)
        nfc, nfd = "r\u00e9sum\u00e9.json", "re\u0301sume\u0301.json"
        for out, summary in ((self.outdir / "records.jsonl", self.outdir / "RECORDS.jsonl"),
                             (self.outdir / nfc, self.outdir / nfd)):
            code, stderr = self.run_lab(out=out, summary=summary)
            self.assertEqual(code, 2, stderr)
            self.assertEqual(list(self.outdir.iterdir()), [])
        code, stderr = self.run_lab(out=self.inputs / "SYNTHETIC_FRAMES.csv")
        self.assertEqual(code, 2, stderr)
        self.assertEqual(self.run_lab(out=self.outdir / "Records.jsonl", summary=self.outdir / "summary.json")[0], 0)

    # P2: directory fsync failures on the commit fail the run --------------------------------------
    @staticmethod
    def failing_directory_fsync(condition=lambda: True):
        """os.fsync that fails for directory descriptors when ``condition()`` holds (files sync normally)."""
        import stat as stat_module
        real_fsync = os.fsync

        def fsync(fd):
            if stat_module.S_ISDIR(os.fstat(fd).st_mode) and condition():
                raise OSError(5, "synthetic directory fsync failure")
            return real_fsync(fd)
        return fsync

    def test_failed_fsync_after_the_summary_rename_withdraws_the_marker(self):
        write_inputs(self.inputs)
        fsync = self.failing_directory_fsync(lambda: self.summary.exists())  # only once the marker is in place
        with mock.patch.object(lab_cli.os, "fsync", fsync):
            code, stderr = self.run_lab()
        self.assertEqual(code, 1, stderr)
        self.assertIn("output_write_error", stderr)
        self.assertFalse(self.summary.exists(), "a failed commit left a success marker")

    def test_failed_fsync_after_the_records_rename_prevents_the_summary(self):
        write_inputs(self.inputs)
        with mock.patch.object(lab_cli.os, "fsync", self.failing_directory_fsync()):
            code, stderr = self.run_lab()
        self.assertEqual(code, 1, stderr)
        self.assertFalse(self.summary.exists())

    # P2: symlink followed by .. ---------------------------------------------------------------------
    def test_destination_parent_is_resolved_like_the_kernel(self):
        write_inputs(self.inputs)
        real = self.root / "real"
        (real / "sub").mkdir(parents=True)
        (self.outdir / "link").symlink_to(real / "sub")
        out = self.outdir / "link" / ".." / "records.jsonl"  # kernel: real/records.jsonl
        temp_dirs, synced = [], []
        real_mkstemp, real_sync = lab_cli.tempfile.mkstemp, lab_cli._fsync_directory

        def mkstemp(*args, **kwargs):
            temp_dirs.append(Path(kwargs["dir"]))
            return real_mkstemp(*args, **kwargs)

        def sync(directory):
            synced.append(Path(directory))
            return real_sync(directory)

        with mock.patch.object(lab_cli.tempfile, "mkstemp", mkstemp), \
             mock.patch.object(lab_cli, "_fsync_directory", sync):
            code, stderr = self.run_lab(out=out)
        self.assertEqual(code, 0, stderr)
        self.assertTrue((real / "records.jsonl").is_file())
        self.assertFalse((self.outdir / "records.jsonl").exists())
        self.assertEqual(temp_dirs[0], real.resolve())
        self.assertIn(real.resolve(), synced)
        self.assertNotIn(self.outdir.resolve(), synced[:1])

    def test_output_names_must_name_a_file(self):
        write_inputs(self.inputs)
        for name in (".", ".."):
            self.assertEqual(self.run_lab(out=Path(str(self.outdir) + os.sep + name))[0], 2)

    # P2: inherited device and socket descriptors ------------------------------------------------------
    def test_inherited_unsafe_descriptors_refuse_the_replay(self):
        write_inputs(self.inputs)
        openers = {
            "character device": lambda: os.open("/dev/zero", os.O_RDONLY),
            "non-local socket": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM),  # never connected
            "kernel control file": lambda: os.open("/proc/self/status", os.O_RDONLY),
        }
        for reason, opener in openers.items():
            handle = opener()
            try:
                code, stderr = self.run_lab()
            finally:
                handle.close() if hasattr(handle, "close") else os.close(handle)
            self.assertEqual(code, 11, (reason, stderr))
            self.assertIn(reason, stderr)
            self.assert_no_output()

    def test_unavailable_proc_fd_enumeration_fails_closed_on_linux(self):
        # The previous implementation returned None on OSError, which was
        # treated as a clean descriptor scan. This test failed on c83025d.
        write_inputs(self.inputs)
        real_listdir = os.listdir

        def denied_proc_fd(path):
            if os.fspath(path) == "/proc/self/fd":
                raise PermissionError(13, "synthetic procfs access denied")
            return real_listdir(path)

        with mock.patch.object(canreplay.os, "listdir", side_effect=denied_proc_fd):
            code, stderr = self.run_lab()
        self.assertEqual(code, 11, stderr)
        self.assertIn("safety_guard", stderr)
        self.assertIn("cannot inspect open file descriptors", stderr)
        self.assert_no_output()

    def test_missing_proc_fd_enumeration_fails_closed_on_linux(self):
        write_inputs(self.inputs)
        real_listdir = os.listdir

        def missing_proc_fd(path):
            if os.fspath(path) == "/proc/self/fd":
                raise FileNotFoundError(2, "synthetic procfs not mounted")
            return real_listdir(path)

        with mock.patch.object(canreplay.os, "listdir", side_effect=missing_proc_fd):
            code, stderr = self.run_lab()
        self.assertEqual(code, 11, stderr)
        self.assertIn("cannot inspect open file descriptors", stderr)
        self.assert_no_output()

    def test_local_pipes_and_unix_sockets_are_allowed(self):
        write_inputs(self.inputs)
        left, right = socket.socketpair(socket.AF_UNIX)
        read_end, write_end = os.pipe()
        try:
            code, stderr = self.run_lab()
        finally:
            left.close(), right.close(), os.close(read_end), os.close(write_end)
        self.assertEqual(code, 0, stderr)

    def test_opening_an_unsafe_descriptor_during_the_replay_is_refused(self):
        fd = os.open("/proc/self/status", os.O_RDONLY)
        duplicate = os.dup(fd)
        try:
            with mock.patch.object(canreplay, "_inherited_descriptor_problems", lambda: []):
                SafetyGuardTests.attempt(self, lambda: os.fdopen(duplicate, "rb", closefd=False).close())
        finally:
            os.close(duplicate)
            os.close(fd)


class WorkflowSafetyTests(unittest.TestCase):
    def test_mcp_ci_does_not_mask_unittest_failure_through_tee(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text()
        step = workflow.split("  fabric-mcp:", 1)[1].split("  compat-openai-agents:", 1)[0]
        segment = step.split("- name: Run the whole suite with the real SDK installed", 1)[1]
        segment = segment.split("- name:", 1)[0]
        self.assertIn("shell: bash", segment)
        self.assertIn(" | tee ", segment)

    def test_pipefail_propagates_failed_producer(self):
        import shutil
        import subprocess
        if not shutil.which("bash"):
            self.skipTest("Bash not available")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail",
                                 "-c", "false | tee /dev/null"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
