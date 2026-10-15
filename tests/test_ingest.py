"""Ingestion: closed schema, strict row syntax, explicit resource limits."""

import os
import random
import tempfile
import unittest
from pathlib import Path

from pexislink.ingest import (CANONICAL_COLUMNS, InputError, Limits, TelemetryFile,
                              parse_number, parse_row, parse_timestamp)

HEADER = "timestamp,vehicle_id,engine_rpm,speed_kmh,coolant_temp_c"
GOOD = "2026-10-02T08:00:00Z,v1,2000,60,90"
KNOWN_ISSUES = {f"{field}_{kind}" for field in CANONICAL_COLUMNS for kind in ("missing", "invalid")}
KNOWN_ISSUES.add("row_field_count")


class FileFixture(unittest.TestCase):
    def write(self, content, *, binary=False):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            handle.write(content if binary else content.encode("utf-8"))
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)


class HeaderTests(FileFixture):
    def test_accepts_required_and_optional_columns_in_any_order(self):
        path = self.write("engine_load_pct,coolant_temp_c,speed_kmh,engine_rpm,vehicle_id,timestamp\n"
                          "30,90,60,2000,v1,2026-10-02T08:00:00Z\n")
        table = TelemetryFile(path)
        self.assertTrue(table.has_load)
        (record,) = table.records()
        self.assertEqual((record.vehicle_id, record.coolant_temp_c, record.engine_load_pct), ("v1", 90.0, 30.0))

    def test_rejects_missing_duplicate_and_unknown_columns(self):
        cases = {
            "timestamp,vehicle_id,engine_rpm,speed_kmh\n": "missing",
            HEADER + ",coolant_temp_c\n": "duplicate",
            HEADER + ",latitude\n": "unknown",
            " " + HEADER + "\n": "missing",  # leading space: exact names only
        }
        for header, message in cases.items():
            with self.subTest(header=header), self.assertRaisesRegex(InputError, message):
                TelemetryFile(self.write(header))

    def test_empty_file_and_bom(self):
        with self.assertRaisesRegex(InputError, "header"):
            TelemetryFile(self.write(""))
        table = TelemetryFile(self.write("﻿" + HEADER + "\n" + GOOD + "\n"))
        self.assertEqual(len(list(table.records())), 1)

    def test_malformed_quoting_is_fatal_with_line_number(self):
        table = TelemetryFile(self.write(HEADER + "\n" + GOOD + "\n" + '2026-10-02T08:01:00Z,"v1,2000,60,90\n'))
        with self.assertRaisesRegex(InputError, "malformed CSV"):
            list(table.records())


class RowSyntaxTests(unittest.TestCase):
    def test_timestamp_profile(self):
        accepted = ["2026-10-02T08:00:00Z", "2026-10-02T08:00:00.5+05:30",
                    "2026-10-02T08:00:00.123456-03:00", "2026-10-02T08:00:00+14:00"]
        rejected = ["2026-10-02T08:00:00", "2026-10-02 08:00:00Z", "20261002T080000Z",
                    "2026-W40-5T08:00Z", "2026-10-02T08Z", "2026-10-02T08:00:60Z",
                    "2026-10-02T08:00:00+14:30", "2026-10-02T08:00:00.1234567Z",
                    "1969-12-31T23:59:59Z", "2200-01-01T00:00:00Z", "2026-02-30T00:00:00Z",
                    "２026-10-02T08:00:00Z", "2026-10-02T08:00:00Z\n"]
        for text in accepted:
            self.assertIsNotNone(parse_timestamp(text), text)
        for text in rejected:
            self.assertIsNone(parse_timestamp(text), repr(text))

    def test_same_instant_in_different_offsets_has_identical_epoch(self):
        utc = parse_timestamp("2026-10-02T08:00:00.000001Z")
        brt = parse_timestamp("2026-10-02T05:00:00.000001-03:00")
        self.assertEqual(utc[1], brt[1])
        self.assertEqual(parse_timestamp("2026-10-02T08:00:00.000002Z")[1] - utc[1], 1)

    def test_numbers_are_strict_ascii_decimals(self):
        for text, value in {"90": 90.0, "-40": -40.0, "+1.5": 1.5, ".5": 0.5, "5.": 5.0, "1e3": 1000.0}.items():
            self.assertEqual(parse_number(text), value, text)
        for text in ("nan", "inf", "-inf", "1e999", "2_000", "٦٠", "９０",
                     "0x10", "1,5", "", " ", "90C", "--1", "1e", "١"):
            self.assertIsNone(parse_number(text), repr(text))

    def test_vehicle_id_alphabet(self):
        def issues(vehicle):
            return parse_row(("2026-10-02T08:00:00Z", vehicle, "2000", "60", "90", ""), 2).issues
        for good in ("v1", "demo-001", "A.b_c:9", "x" * 64):
            self.assertNotIn("vehicle_id_invalid", issues(good), good)
        for bad in ("v\x1b[31m", "v\x001", "José", "x" * 65, "-lead", "has space", "v1;drop"):
            self.assertIn("vehicle_id_invalid", issues(bad), repr(bad))
        self.assertIn("vehicle_id_missing", issues("  "))

    def test_row_level_problems_are_issues_not_exceptions(self):
        record = parse_row(("not-a-time", "", "fast", "", "nan", "1_0"), 7)
        self.assertEqual(record.line, 7)
        self.assertEqual(set(record.issues), {"timestamp_invalid", "vehicle_id_missing",
                                              "engine_rpm_invalid", "speed_kmh_missing",
                                              "coolant_temp_c_invalid", "engine_load_pct_invalid"})
        self.assertEqual(parse_row(("a", "b"), 3).issues, ("row_field_count",))

    def test_optional_column_absent_vs_present_but_empty(self):
        present_empty = parse_row(("2026-10-02T08:00:00Z", "v1", "2000", "60", "90", ""), 2)
        self.assertIn("engine_load_pct_missing", present_empty.issues)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as handle:
            handle.write(HEADER + "\n" + GOOD + "\n")
        self.addCleanup(os.unlink, handle.name)
        (absent,) = TelemetryFile(handle.name).records()
        self.assertIsNone(absent.engine_load_pct)
        self.assertEqual(absent.issues, ())

    def test_fuzz_parse_row_never_raises_and_uses_known_codes(self):
        rng = random.Random(20261015)
        alphabet = "0123456789.:-+TZeE ,\"x\x00٠０\n\t_"
        for _ in range(3000):
            width = rng.choice((5, 6, 6, 6, 7))
            values = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25))) for _ in range(width)]
            record = parse_row(values, 2)
            self.assertTrue(set(record.issues) <= KNOWN_ISSUES, record.issues)
            if record.timestamp is not None:
                self.assertIsNotNone(record.epoch_us)


class LimitTests(FileFixture):
    def test_file_size_limit(self):
        path = self.write(HEADER + "\n" + (GOOD + "\n") * 100)
        with self.assertRaisesRegex(InputError, "exceeds"):
            TelemetryFile(path, Limits(max_file_bytes=1000))

    @unittest.skipUnless(os.path.exists("/dev/zero"), "requires /dev/zero")
    def test_endless_device_is_bounded(self):
        with self.assertRaisesRegex(InputError, "exceeds"):
            TelemetryFile("/dev/zero", Limits(max_file_bytes=1 << 20))

    def test_line_length_limit(self):
        table = TelemetryFile(self.write(HEADER + "\n" + "2026-10-02T08:00:00Z," + "v" * 5000 + ",1,1,1\n"))
        with self.assertRaisesRegex(InputError, "longer than"):
            list(table.records())

    def test_row_limit(self):
        table = TelemetryFile(self.write(HEADER + "\n" + (GOOD + "\n") * 11), Limits(max_rows=10))
        with self.assertRaisesRegex(InputError, "more than 10"):
            list(table.records())

    def test_invalid_utf8_reports_line(self):
        data = (HEADER + "\n" + GOOD + "\n").encode() + b"2026-10-02T08:01:00Z,v\xff,1,1,1\n"
        with self.assertRaisesRegex(InputError, "line 3: invalid UTF-8"):
            TelemetryFile(self.write(data, binary=True))

    def test_limits_validate_themselves(self):
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                Limits(max_rows=bad)


class SnapshotTests(FileFixture):
    def test_records_are_reparsed_deterministically_from_an_immutable_snapshot(self):
        path = self.write(HEADER + "\n" + GOOD + "\n")
        table = TelemetryFile(path)
        path.write_text(HEADER + "\n2026-10-02T09:00:00Z,v9,1,1,1\n", encoding="utf-8")  # mutate on disk
        first, second = list(table.records()), list(table.records())
        self.assertEqual(first, second)
        self.assertEqual(first[0].vehicle_id, "v1")
        self.assertEqual(len(table.sha256), 64)

    def test_line_endings_and_quoted_newlines_match_csv_module_semantics(self):
        import csv as csv_module
        import io
        from pexislink.ingest import _bounded_lines
        bodies = [
            HEADER + "\n" + GOOD + "\n",
            HEADER + "\r\n" + GOOD + "\r\n",
            HEADER + "\r" + GOOD + "\r",                 # classic Mac line endings
            HEADER + "\n" + GOOD,                         # no final newline
            HEADER + "\n" + '2026-10-02T08:00:00Z,"v\n1",2000,60,90\n',  # quoted newline
            "\n\n" + HEADER + "\n\n" + GOOD + "\n\n",
        ]
        for body in bodies:
            expected = list(csv_module.reader(io.StringIO(body, newline=""), strict=True))
            actual = list(csv_module.reader(_bounded_lines(body, 1024), strict=True))
            self.assertEqual(actual, expected, repr(body))

    def test_line_numbers_are_physical(self):
        table = TelemetryFile(self.write(HEADER + "\n\n" + GOOD + "\n" + "2026-10-02T08:01:00Z,v1,2000,60,91\n"))
        self.assertEqual([r.line for r in table.records()], [3, 4])


if __name__ == "__main__":
    unittest.main()
