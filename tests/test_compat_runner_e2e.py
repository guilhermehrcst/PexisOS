"""End-to-end gates for the multi-client runner and the Codex driver (standard library only).

Complements test_compat_runner.py, which checks the error lists: here the real runner process must
exit non-zero when a required case is missing, and a failed Codex start must not leak a process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "compat" / "run_clients.py"


class RequireExitStatusTests(unittest.TestCase):
    def test_require_exits_nonzero_when_a_required_case_is_missing(self):
        """The real runner, with the Codex suite replaced by one whose C13 was deleted (C01-C12 pass)."""
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            code = (
                "import runpy, sys, types, unittest\n"
                f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
                "import compat\n"
                "m = types.ModuleType('compat.test_codex_app_server')\n"
                "def ok(self): pass\n"
                "m.T = type('T', (unittest.TestCase,), {f'test_C{i:02d}_x': ok for i in range(1, 13)})\n"
                "sys.modules[m.__name__] = m; compat.test_codex_app_server = m\n"
                f"sys.argv = ['run_clients.py', '--require', 'codex-cli', '--json', {str(report)!r}]\n"
                f"runpy.run_path({str(RUNNER)!r}, run_name='__main__')\n"
            )
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600,
                                    env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")})
            self.assertEqual(result.returncode, 1, result.stderr[-2000:])
            codex = json.loads(report.read_text())["clients"]["codex-cli"]
        self.assertTrue(any("C13" in e for e in codex["required_errors"]), codex["required_errors"])
        self.assertTrue(any("missing_test_ids: C13" in e for e in codex["integrity_errors"]))


class DriverPartialStartTests(unittest.TestCase):
    def test_failed_initialize_tears_down_the_started_process(self):
        """If ``initialize`` fails after Popen, the constructor must not leak the process or its pipes."""
        sys.path.insert(0, str(ROOT / "tests" / "compat"))
        import _codex_app_server as driver

        started = []
        real_popen = driver.subprocess.Popen

        def recording_popen(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            return started[-1]

        driver.subprocess.Popen = recording_popen
        try:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
                driver.CodexAppServer("/bin/true", Path(tmp), {}, timeout=10)  # exits before answering
        finally:
            driver.subprocess.Popen = real_popen
        [proc] = started
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed and proc.stdout.closed)


FAKE_CODEX = r"""
import json, os, sys
mode = os.environ["FAKE_MODE"]
SECRET = "SECRET-PAYLOAD-7f3a"
bad = {"invalid_json": SECRET + " this is not json\n",
       "not_object": json.dumps([SECRET, 1]) + "\n",
       "not_jsonrpc": json.dumps({"payload": SECRET}) + "\n"}
out = sys.stdout.buffer
def send(raw):
    out.write(raw if isinstance(raw, bytes) else raw.encode()); out.flush()
for count, line in enumerate(sys.stdin, 1):
    request = json.loads(line)
    if "id" not in request:
        continue  # the initialized notification
    late = mode.startswith("late_")
    kind = mode[len("late_"):] if late else mode
    if (count == 1 and not late) or (count > 1 and late):
        if kind == "exit":
            send(bad["invalid_json"])
            sys.exit(0)  # a violation followed by the process going away
        if kind == "bad_utf8":
            send(b"\xff\xfe" + SECRET.encode() + b"\n")
        elif kind != "ok":
            send(bad[kind])
            continue
    send(json.dumps({"id": request["id"], "result": {"echo": request["method"]}}) + "\n")
"""


class DriverStdoutProtocolTests(unittest.TestCase):
    """Codex app-server stdout must be JSON-RPC objects. Any other line is a protocol violation that
    fails the pending request at once (never a 90 s timeout), without echoing the line's content."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests" / "compat"))
        import _codex_app_server as driver
        cls.driver = driver

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name) / "fake-codex"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
        self.fake.chmod(0o755)
        self.started = []
        real_popen = self.driver.subprocess.Popen

        def recording_popen(*args, **kwargs):
            self.started.append(real_popen(*args, **kwargs))
            return self.started[-1]

        self.driver.subprocess.Popen = recording_popen
        self.addCleanup(setattr, self.driver.subprocess, "Popen", real_popen)

    def start(self, mode, timeout=20):
        return self.driver.CodexAppServer(str(self.fake), Path(self.tmp.name) / mode, {},
                                          env={"FAKE_MODE": mode}, timeout=timeout)

    def assert_fails_fast_and_quietly(self, action):
        began = time.monotonic()
        with self.assertRaises(Exception) as caught:
            action()
        elapsed = time.monotonic() - began
        self.assertEqual(type(caught.exception).__name__, "ProtocolError", repr(caught.exception))
        self.assertLess(elapsed, 10, "a protocol violation must not wait for the request timeout")
        self.assertNotIn("SECRET-PAYLOAD", str(caught.exception))
        return caught.exception

    def assert_process_reaped(self):
        for proc in self.started:
            self.assertIsNotNone(proc.poll())
            self.assertTrue(proc.stdin.closed and proc.stdout.closed)

    def test_invalid_json_line_during_initialize_fails_fast(self):
        self.assert_fails_fast_and_quietly(lambda: self.start("invalid_json"))
        self.assert_process_reaped()

    def test_json_that_is_not_an_object_fails_fast(self):
        self.assert_fails_fast_and_quietly(lambda: self.start("not_object"))
        self.assert_process_reaped()

    def test_object_that_is_not_a_jsonrpc_message_fails_fast(self):
        self.assert_fails_fast_and_quietly(lambda: self.start("not_jsonrpc"))
        self.assert_process_reaped()

    def test_undecodable_stdout_stream_fails_fast(self):
        self.assert_fails_fast_and_quietly(lambda: self.start("bad_utf8"))
        self.assert_process_reaped()

    def test_violation_after_initialize_fails_this_and_later_requests(self):
        server = self.start("late_invalid_json")
        try:
            self.assert_fails_fast_and_quietly(lambda: server.request("mcpServerStatus/list", {}))
            self.assert_fails_fast_and_quietly(lambda: server.request("thread/start", {}))  # sticky
            self.assertNotIn("SECRET-PAYLOAD", repr(server.notifications))
        finally:
            server.close()
        self.assert_process_reaped()

    def test_a_dead_server_after_a_violation_still_reports_the_violation(self):
        server = self.start("late_exit")
        try:
            self.assert_fails_fast_and_quietly(lambda: server.request("mcpServerStatus/list", {}))
            server.proc.wait(timeout=10)  # stdin is now a broken pipe
            self.assert_fails_fast_and_quietly(lambda: server.request("thread/start", {}))
        finally:
            server.close()
        self.assert_process_reaped()

    def test_well_formed_traffic_is_unchanged(self):
        server = self.start("ok")
        try:
            self.assertEqual(server.initialize_result, {"echo": "initialize"})
            self.assertEqual(server.request("mcpServerStatus/list", {}), {"echo": "mcpServerStatus/list"})
        finally:
            code, _ = server.close()
        self.assertEqual(code, 0)
        self.assert_process_reaped()


if __name__ == "__main__":
    unittest.main()
