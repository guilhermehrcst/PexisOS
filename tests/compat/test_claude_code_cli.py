"""Interoperability: Claude Code (CLI) as a client of the PexisLink MCP stdio server.

Optional, local and test-only. Claude Code is proprietary software. It is never installed by this
repository or by CI: these tests run only when ``PEXISLINK_CLAUDE_BIN`` points at an existing
installation, and are otherwise reported as NOT_RUN.

Only the documented, model-free ``claude mcp`` configuration commands are used (``add``, ``get``,
``list``) plus ``--version``. ``claude mcp list``/``get`` run a health check: they launch the configured
stdio server and connect to it. Any other invocation could start a model turn, so ``_claude`` refuses
every other argument vector. No tool is listed or called through Claude Code here, because Claude
Code exposes MCP tools only inside a model turn (authentication and billable model calls). Those
cases are reported as NOT_RUN, never as passed.

Containment: an explicit environment (no host variables beyond ``PATH``), a fresh ``HOME`` and
``CLAUDE_CONFIG_DIR`` under a temporary directory, non-essential traffic, telemetry and the
auto-updater disabled, and every HTTP(S) proxy variable pointing at an unroutable local port.

Test names carry the multi-client matrix id (K01..K08). ``run_clients.py`` maps outcomes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CLAUDE = os.environ.get("PEXISLINK_CLAUDE_BIN", "")
HAVE_CLAUDE = bool(CLAUDE) and os.path.isfile(CLAUDE) and os.access(CLAUDE, os.X_OK)
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIXTURE = Path(__file__).with_name("_fixture_server.py")
GUEST = "demo:principal:guest"
CANARY = "PEXISLINK_COMPAT_CANARY_SECRET"
UNROUTABLE = "http://127.0.0.1:9"
NOT_RUN = ("not_run: Claude Code not available (set PEXISLINK_CLAUDE_BIN to an existing installation) "
           "or the MCP SDK is not installed")
MODEL_TURN = ("not_run: Claude Code exposes MCP tool listing and tool calls only inside a model turn, which "
              "needs authentication and billable model calls; this case is covered for the same server by "
              "the Codex and OpenAI Agents SDK suites, not by Claude Code")
ALLOWED = {("--version",), ("mcp", "add"), ("mcp", "get"), ("mcp", "list")}


@unittest.skipUnless(HAVE_CLAUDE and importlib.util.find_spec("mcp") is not None, NOT_RUN)
class ClaudeCodeCliInteropTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for name in ("home", "config", "project"):
            (self.tmp / name).mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _claude(self, *args: str, extra_env: dict | None = None, timeout: float = 180) -> subprocess.CompletedProcess:
        if args[:1] != ("--version",) and args[:2] not in ALLOWED:
            raise AssertionError(f"refusing a Claude Code invocation that could start a model turn: {args}")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp / "home"),
               "CLAUDE_CONFIG_DIR": str(self.tmp / "config"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "NO_PROXY": "", "no_proxy": ""}
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
            env[name] = UNROUTABLE
        env.update(extra_env or {})
        return subprocess.run([CLAUDE, *args], capture_output=True, text=True, timeout=timeout, env=env,
                              cwd=str(self.tmp / "project"))

    def add(self, name: str, *server_args: str) -> None:
        result = self._claude("mcp", "add", name, "--scope", "local", "-e", f"PYTHONPATH={SRC}", "-e",
                              "PYTHONNOUSERSITE=1", "--", sys.executable, *server_args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def status_line(self, output: str, name: str) -> str:
        lines = [line for line in output.splitlines() if line.startswith(f"{name}: ")]
        self.assertEqual(len(lines), 1, output)
        return lines[0]

    # K01 -------------------------------------------------------------------------------------------
    def test_K01_configuration_round_trip_stays_in_the_isolated_config(self):
        self.add("pexislink", "-m", "pexislink", "fabric", "mcp-stdio", "--principal", GUEST)
        got = self._claude("mcp", "get", "pexislink")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("Type: stdio", got.stdout)
        self.assertIn(f"Command: {sys.executable}", got.stdout)
        self.assertIn(f"Args: -m pexislink fabric mcp-stdio --principal {GUEST}", got.stdout)
        self.assertTrue((self.tmp / "config" / ".claude.json").is_file())

    # K02 -------------------------------------------------------------------------------------------
    def test_K02_health_check_launches_and_connects_to_the_server(self):
        """``Connected`` is the client's health check (process launched, MCP connection established).
        The server-side env dump proves the process really ran; it is not a tool call."""
        dump, log = self.tmp / "env.json", self.tmp / "audit.log"
        self.add("pexislink", str(FIXTURE), "--principal", GUEST, "--env-dump", str(dump), "--audit-log", str(log))
        listed = self._claude("mcp", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        line = self.status_line(listed.stdout, "pexislink")
        self.assertIn("Connected", line)
        self.assertNotIn("Failed", line)
        self.assertTrue(dump.is_file(), "Connected was reported but the server never started")
        unix = f"socket.__new__ family={int(socket.AF_UNIX)}"
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        unexpected = [entry for entry in log.read_text().splitlines() if entry != unix and not (
            entry.startswith("open /dev/null ") and not int(entry.split("flags=")[1].split()[0]) & write_flags)]
        self.assertEqual(unexpected, [])

    # K03 -------------------------------------------------------------------------------------------
    def test_K03_a_broken_server_is_reported_as_failed_not_connected(self):
        self.add("pexislink", "-m", "pexislink", "fabric", "mcp-stdio", "--principal", GUEST)
        self.add("broken", "-m", "pexislink", "fabric", "mcp-stdio", "--principal", "NOT VALID")
        listed = self._claude("mcp", "list")
        self.assertIn("Connected", self.status_line(listed.stdout, "pexislink"))
        broken = self.status_line(listed.stdout, "broken")
        self.assertIn("Failed", broken)
        self.assertNotIn("✔", broken)

    # K04 -------------------------------------------------------------------------------------------
    def test_K04_unapproved_project_server_is_not_started(self):
        """A project-scoped ``.mcp.json`` server needs explicit approval; until then Claude Code must not
        launch it (documented in ``claude mcp --help``). Measured with a server-side env dump."""
        dump = self.tmp / "project-env.json"
        config = {"mcpServers": {"project-pexislink": {
            "command": sys.executable, "args": [str(FIXTURE), "--principal", GUEST, "--env-dump", str(dump)],
            "env": {"PYTHONPATH": str(SRC)}}}}
        (self.tmp / "project" / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")
        listed = self._claude("mcp", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn("Connected", self.status_line(listed.stdout, "project-pexislink"))
        self.assertFalse(dump.exists(), "an unapproved project server was started")

    # K05 -------------------------------------------------------------------------------------------
    def test_K05_client_environment_reaches_the_server_finding_I7(self):
        """Measured interoperability finding I7 (pinned so a client change is noticed): Claude Code
        passes its own environment to stdio MCP servers, so a variable set for the client (here a
        canary) is visible to the PexisLink process. PexisLink never returns environment values, but
        operators must not run the client with secrets they would not give the server."""
        dump = self.tmp / "env.json"
        self.add("pexislink", str(FIXTURE), "--principal", GUEST, "--env-dump", str(dump))
        self._claude("mcp", "list", extra_env={CANARY: "client-side-secret"})
        names = set(json.loads(dump.read_text()))
        self.assertIn(CANARY, names)
        self.assertTrue({"PYTHONPATH", "PYTHONNOUSERSITE", "PATH", "HOME"} <= names)

    # K06 -------------------------------------------------------------------------------------------
    def test_K06_health_check_leaves_no_server_process(self):
        marker = str(self.tmp / "lifecycle-marker.json")
        self.add("pexislink", str(FIXTURE), "--principal", GUEST, "--env-dump", marker)
        self.assertEqual(self._claude("mcp", "list").returncode, 0)
        for _ in range(50):
            alive = [p.name for p in Path("/proc").iterdir() if p.name.isdigit() and _cmdline_has(p, marker)]
            if not alive:
                break
            time.sleep(0.1)
        self.assertEqual(alive, [], "server process left behind after the health check")
        self.assertTrue(Path(marker).is_file())

    # K07 / K08 -------------------------------------------------------------------------------------
    def test_K07_tool_discovery_through_claude_code(self):
        self.skipTest(MODEL_TURN)

    def test_K08_tool_calls_through_claude_code(self):
        """Authorized query, denied access, forged identity, expiry/revocation, isolation, pagination,
        sanitization and determinism, as seen by Claude Code."""
        self.skipTest(MODEL_TURN)


def _cmdline_has(proc: Path, marker: str) -> bool:
    try:
        return marker in (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False


class ClaudeCodeNotRunReport(unittest.TestCase):
    def test_report_whether_claude_code_ran(self):
        if not HAVE_CLAUDE:
            print("\n[compat] Claude Code interoperability: not_run (PEXISLINK_CLAUDE_BIN unset)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
