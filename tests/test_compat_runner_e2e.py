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


if __name__ == "__main__":
    unittest.main()
