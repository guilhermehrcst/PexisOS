"""Interoperability: the Codex CLI as a client of the PexisLink MCP stdio server.

Optional and test-only. Requires the pinned Codex CLI (``npm ci --ignore-scripts --prefix
requirements/compat-codex``, Apache-2.0) and the MCP SDK for the server
(``requirements/fabric-mcp.txt``). Set ``PEXISLINK_CODEX_BIN`` to the ``codex`` executable. Without it
every Codex test is skipped and reported as NOT_RUN.

Codex is driven through its documented (experimental) ``codex app-server`` JSON-RPC interface and its
``codex mcp`` configuration commands, with model-free requests only (see ``_codex_app_server.py``):
no turn is started, no model is called, no credential exists, and outbound traffic is blocked by two
independent barriers. Every tool result below was produced by the PexisLink core and relayed by Codex.

Test names carry the multi-client matrix id (C01..C13). ``run_clients.py`` maps outcomes to
PASS/FAIL/NOT_RUN/NOT_SUPPORTED. Authorization is decided only by the server (``Session.call``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _codex_app_server import CodexAppServer, RpcError, run_cli  # noqa: E402

CODEX = os.environ.get("PEXISLINK_CODEX_BIN", "")
HAVE_CODEX = bool(CODEX) and os.path.isfile(CODEX) and os.access(CODEX, os.X_OK)
HAVE_MCP = importlib.util.find_spec("mcp") is not None
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIXTURE = Path(__file__).with_name("_fixture_server.py")
TOOLS = {"describe_entity", "explain_assessment", "list_capabilities", "list_entities", "read_observations"}
FRIDGE, CAR, API = "demo:appliance:fridge-01", "demo:vehicle:synthetic-01", "demo:service:checkout-api"
GUEST, ANALYST = "demo:principal:guest", "demo:principal:analyst"
CANARY = "PEXISLINK_COMPAT_CANARY_SECRET"
NOT_RUN = ("not_run: Codex CLI not available (set PEXISLINK_CODEX_BIN after "
           "npm ci --ignore-scripts --prefix requirements/compat-codex) or the MCP SDK is not installed")


def server(principal: str = GUEST, *, fixture_args: list[str] | None = None, policy: str | None = None) -> dict:
    if fixture_args is not None:
        args = [str(FIXTURE), "--principal", principal, *fixture_args]
    else:
        args = ["-m", "pexislink", "fabric", "mcp-stdio", "--principal", principal]
        if policy:
            args += ["--policy", policy]
    return {"command": sys.executable, "args": args, "env": {"PYTHONPATH": str(SRC), "PYTHONNOUSERSITE": "1"}}


def body(result: dict) -> dict:
    """The JSON text block; Codex must relay it unchanged next to structuredContent when present."""
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text", result
    parsed = json.loads(result["content"][0]["text"])
    if result.get("structuredContent") is not None:
        assert result["structuredContent"] == parsed, "structuredContent differs from the text block"
    return parsed


def children_with(marker: str) -> list[str]:
    found = []
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if entry.name.isdigit():
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if marker in cmdline:
                found.append(entry.name)
    return found


@unittest.skipUnless(HAVE_CODEX and HAVE_MCP, NOT_RUN)
class CodexAppServerInteropTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def codex(self, servers: dict[str, dict], **kwargs) -> CodexAppServer:
        self._n += 1
        client = CodexAppServer(CODEX, self.tmp / f"codex-{self._n}", servers, **kwargs)
        self.addCleanup(lambda: client.proc.poll() is None and client.close())
        return client

    def assertNoEgress(self, client: CodexAppServer):
        self.assertEqual(client.egress_lines(), [], "Codex attempted an outbound request")

    # C01 -------------------------------------------------------------------------------------------
    def test_C01_initialize_and_server_identity(self):
        from pexislink.fabric.runtime import FABRIC_VERSION

        version = run_cli(CODEX, self.tmp / "v", "--version").stdout.split()[-1]
        with self.codex({"pexislink": server()}) as client:
            init = client.initialize_result
            [status] = client.status("pexislink")
        self.assertTrue(init["userAgent"].startswith(f"pexislink-compat/{version} "), init["userAgent"])
        self.assertEqual((status["serverInfo"]["name"], status["serverInfo"]["version"]),
                         ("pexislink-agent-fabric", FABRIC_VERSION))
        self.assertEqual(status["serverCapabilities"]["tools"], {"listChanged": False})
        self.assertNoEgress(client)

    def test_C01_negotiated_mcp_protocol_version_is_observable(self):
        self.skipTest("not_supported: Codex app-server reports serverInfo and capabilities, but no public "
                      "request exposes the negotiated MCP protocol version")

    # C02 -------------------------------------------------------------------------------------------
    def test_C02_configured_is_not_connected_is_not_executed(self):
        """Three separate facts, each measured on its own: (1) ``codex mcp add`` writes configuration
        and ``codex mcp list`` reports it without starting anything; (2) a configured but broken server
        is reported as failed by discovery, with no tools; (3) a tool call executes in the core."""
        cli_root = self.tmp / "cli"
        spec = server(fixture_args=["--env-dump", str(self.tmp / "started.json")])
        added = run_cli(CODEX, cli_root, "mcp", "add", "pexislink", "--env", f"PYTHONPATH={SRC}", "--",
                        spec["command"], *spec["args"])
        self.assertEqual(added.returncode, 0, added.stderr)
        listed = run_cli(CODEX, cli_root, "mcp", "list", "--json")
        [entry] = json.loads(listed.stdout)
        self.assertEqual((entry["name"], entry["transport"]["type"], entry["transport"]["args"]),
                         ("pexislink", "stdio", spec["args"]))
        self.assertFalse((self.tmp / "started.json").exists(), "codex mcp list started the server")

        broken = server("NOT VALID")  # the CLI refuses the principal and exits before initializing
        with self.codex({"broken": broken, "pexislink": server(ANALYST)}) as client:
            statuses = {s["name"]: s for s in client.status()}
            thread = client.start_thread()
            with self.assertRaises(RpcError):
                client.call(thread, "broken", "list_entities", {})
            executed = client.call(thread, "pexislink", "explain_assessment", {"entity_id": FRIDGE})
        self.assertEqual(statuses["broken"]["tools"], {})
        self.assertIsNone(statuses["broken"]["serverInfo"])
        self.assertIn("MCP startup failed", statuses["broken"]["toolsError"])
        self.assertEqual(set(statuses["pexislink"]["tools"]), TOOLS)
        self.assertFalse(executed["isError"])
        self.assertEqual(body(executed)["entity_id"], FRIDGE)
        failed = [n["params"]["name"] for n in client.notifications
                  if n.get("method") == "mcpServer/startupStatus/updated" and n["params"]["status"] == "failed"]
        self.assertEqual(failed, ["broken"])
        self.assertNoEgress(client)

    # C03 -------------------------------------------------------------------------------------------
    def test_C03_discover_exactly_five_read_only_tools_with_core_schemas(self):
        from pexislink.fabric.runtime import FabricLimits, tool_specs

        with self.codex({"pexislink": server()}) as client:
            [status] = client.status("pexislink")
        tools = status["tools"]
        self.assertEqual(set(tools), TOOLS)
        expected = {name: spec["input_schema"] for name, spec in tool_specs(FabricLimits()).items()}
        self.assertEqual({name: tool["inputSchema"] for name, tool in tools.items()}, expected)
        for name, tool in tools.items():
            self.assertEqual(tool["annotations"], {"readOnlyHint": True, "destructiveHint": False,
                                                   "idempotentHint": True, "openWorldHint": False}, name)

    # C04 -------------------------------------------------------------------------------------------
    def test_C04_authorized_query_executes(self):
        with self.codex({"pexislink": server(ANALYST)}) as client:
            result = client.call(client.start_thread(), "pexislink", "read_observations",
                                 {"entity_id": FRIDGE, "mode": "latest"})
        self.assertFalse(result["isError"])
        data = body(result)
        self.assertEqual(data["observation"]["entity_id"], FRIDGE)
        self.assertEqual(data["observation"]["schema_version"], "pexislink.observation.v0")
        self.assertTrue(data["freshness"]["current"])

    # C05 -------------------------------------------------------------------------------------------
    def test_C05_denied_access_is_hidden_and_indistinguishable(self):
        with self.codex({"pexislink": server(GUEST)}) as client:
            thread = client.start_thread()
            listing = client.call(thread, "pexislink", "list_entities", {})
            hidden = client.call(thread, "pexislink", "describe_entity", {"entity_id": CAR})
            absent = client.call(thread, "pexislink", "describe_entity", {"entity_id": "demo:vehicle:nope"})
        self.assertEqual([e["entity_id"] for e in body(listing)["entities"]], [FRIDGE])
        self.assertTrue(hidden["isError"] and absent["isError"])
        self.assertEqual(hidden["content"], absent["content"])
        self.assertEqual(body(hidden)["error"]["code"], "not_found")

    # C06 -------------------------------------------------------------------------------------------
    def test_C06_forged_identity_in_arguments_is_rejected(self):
        with self.codex({"pexislink": server(GUEST)}) as client:
            thread = client.start_thread()
            forged = [client.call(thread, "pexislink", name, args) for name, args in (
                ("list_entities", {"principal": ANALYST}),
                ("describe_entity", {"entity_id": CAR, "principal": ANALYST}),
                ("read_observations", {"entity_id": CAR, "mode": "latest", "as_principal": ANALYST}),
                ("describe_entity", {"entity_id": CAR, "_meta": {"principal": ANALYST}}))]
            meta = client.request("mcpServer/tool/call", {"threadId": thread, "server": "pexislink",
                                                          "tool": "describe_entity", "arguments": {"entity_id": CAR},
                                                          "_meta": {"principal": ANALYST}})
            after = client.call(thread, "pexislink", "describe_entity", {"entity_id": CAR})
        for result in forged:
            self.assertEqual(body(result)["error"]["code"], "invalid_arguments")
        self.assertEqual(body(meta)["error"]["code"], "not_found")  # MCP _meta carries no identity
        self.assertEqual(body(after)["error"]["code"], "not_found")

    # C07 -------------------------------------------------------------------------------------------
    def test_C07_expired_permission_is_denied(self):
        from pexislink.fabric.adapters._common import T0
        from pexislink.fabric.policy import Grant, grants_to_document

        day = 86_400 * 1_000_000
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps(grants_to_document([Grant("op:grant:old", "op:principal:fleet", CAR,
                                                               frozenset({"describe", "assess"}),
                                                               T0 - 10 * day, T0 - day)])), encoding="utf-8")
        with self.codex({"pexislink": server("op:principal:fleet", policy=str(policy))}) as client:
            result = client.call(client.start_thread(), "pexislink", "explain_assessment", {"entity_id": CAR})
        self.assertEqual(body(result)["error"]["code"], "access_denied")
        self.assertEqual(body(result)["error"]["reason"], "expired")

    def test_C07_revocation_takes_effect_within_a_live_codex_session(self):
        spec = server(ANALYST, fixture_args=["--revoke-after", "1", "demo:grant:analyst-fridge"])
        with self.codex({"pexislink": spec}) as client:
            thread = client.start_thread()
            before = client.call(thread, "pexislink", "explain_assessment", {"entity_id": FRIDGE})
            after = client.call(thread, "pexislink", "explain_assessment", {"entity_id": FRIDGE})
        self.assertFalse(before["isError"])
        self.assertEqual(body(after)["error"]["reason"], "revoked")

    # C08 -------------------------------------------------------------------------------------------
    def test_C08_two_principals_in_one_codex_thread_stay_isolated(self):
        """Identical tool names on two servers: Codex routes by server name, never ambiguously."""
        with self.codex({"guest": server(GUEST), "analyst": server(ANALYST)}) as client:
            thread = client.start_thread()
            before = client.call(thread, "guest", "list_entities", {})
            analyst = client.call(thread, "analyst", "list_entities", {})
            car = client.call(thread, "analyst", "describe_entity", {"entity_id": CAR})
            denied = client.call(thread, "guest", "describe_entity", {"entity_id": CAR})
            after = client.call(thread, "guest", "list_entities", {})
        self.assertEqual(before["content"], after["content"])
        self.assertEqual([e["entity_id"] for e in body(before)["entities"]], [FRIDGE])
        self.assertEqual(sorted(e["entity_id"] for e in body(analyst)["entities"]), sorted([FRIDGE, CAR, API]))
        self.assertEqual(body(car)["entity"]["entity_id"], CAR)
        self.assertEqual(body(denied)["error"]["code"], "not_found")

    # C09 -------------------------------------------------------------------------------------------
    def test_C09_pagination_cursors_and_invalid_arguments(self):
        with self.codex({"pexislink": server(ANALYST)}) as client:
            thread = client.start_thread()
            pages, cursor = [], None
            while True:
                args = {"limit": 1} | ({"cursor": cursor} if cursor else {})
                page = body(client.call(thread, "pexislink", "list_entities", args))
                pages.append([e["entity_id"] for e in page["entities"]])
                cursor = page["next_cursor"]
                if cursor is None or len(pages) > 5:
                    break
            first = body(client.call(thread, "pexislink", "read_observations",
                                     {"entity_id": FRIDGE, "mode": "history", "limit": 4}))
            second = body(client.call(thread, "pexislink", "read_observations",
                                      {"entity_id": FRIDGE, "mode": "history", "limit": 4,
                                       "cursor": first["next_cursor"]}))
            bad = [client.call(thread, "pexislink", "read_observations", args) for args in (
                {"entity_id": FRIDGE, "mode": "history", "cursor": "abc"},
                {"entity_id": FRIDGE, "mode": "history", "cursor": "9" * 19},
                {"entity_id": FRIDGE, "mode": "latest", "limit": 1},
                {"entity_id": FRIDGE, "mode": "latest", "cursor": None, "limit": None})]  # I2 via Codex
        self.assertEqual(pages, [[FRIDGE], [API], [CAR]])
        self.assertEqual(second["items"][0]["seq"], first["items"][-1]["seq"] + 1)
        for result in bad:
            self.assertEqual(body(result)["error"]["code"], "invalid_arguments")

    # C10 -------------------------------------------------------------------------------------------
    def test_C10_errors_are_sanitized_and_no_host_variable_reaches_the_server(self):
        """Measured: Codex passes the server exactly its configured ``env`` plus HOME and PATH (and the
        child interpreter may add LC_CTYPE, PEP 538). A canary in Codex's own environment is not
        forwarded."""
        marker = "SECRET-PATH-/home/operator/.ssh/id_rsa"
        dump = self.tmp / "env.json"
        with self.codex({"pexislink": server(GUEST, fixture_args=["--env-dump", str(dump)])},
                        env={CANARY: "s3cr3t-value"}) as client:
            thread = client.start_thread()
            results = [client.call(thread, "pexislink", name, args) for name, args in (
                ("describe_entity", {"entity_id": marker}),
                ("read_observations", {"entity_id": FRIDGE, "mode": "history", "cursor": marker}),
                ("no_such_tool", {"path": marker}),
                ("explain_assessment", {"entity_id": FRIDGE, "signal": "nope"}))]
        for result in results:
            self.assertTrue(result["isError"])
            text = json.dumps(result)
            for leak in ("SECRET", "/home/", "id_rsa", "s3cr3t", "Traceback", str(SRC)):
                self.assertNotIn(leak, text)
        names = set(json.loads(dump.read_text()))
        self.assertNotIn(CANARY, names)
        expected = {"HOME", "PATH", "PYTHONPATH", "PYTHONNOUSERSITE"}
        self.assertIn(names, (expected, expected | {"LC_CTYPE"}), sorted(names))

    # C11 -------------------------------------------------------------------------------------------
    def test_C11_results_are_byte_identical_across_codex_and_server_processes(self):
        calls = [("list_entities", {}), ("explain_assessment", {"entity_id": FRIDGE}),
                 ("read_observations", {"entity_id": CAR, "mode": "history", "limit": 30}),
                 ("read_observations", {"entity_id": API, "mode": "latest"}),
                 ("list_capabilities", {"entity_id": CAR})]

        def once():
            with self.codex({"pexislink": server(ANALYST)}) as client:
                thread = client.start_thread()
                return [client.call(thread, "pexislink", n, a)["content"][0]["text"] for n, a in calls]

        self.assertEqual(once(), once())

    # C12 -------------------------------------------------------------------------------------------
    def test_C12_no_write_tools_and_no_effects_in_server_or_client(self):
        """The server-side CPython audit log (every socket, process, native-loading and device event,
        see _audit.py) may contain only the measured AF_UNIX socketpair and a read-only /dev/null open.
        Codex is native code and cannot be audited this way; its log must show no outbound request."""
        log = self.tmp / "audit.log"
        with self.codex({"pexislink": server(ANALYST, fixture_args=["--audit-log", str(log)])}) as client:
            thread = client.start_thread()
            for name in sorted(TOOLS):
                args = {} if name == "list_entities" else {"entity_id": CAR}
                if name == "read_observations":
                    args["mode"] = "history"
                self.assertFalse(client.call(thread, "pexislink", name, args)["isError"], name)
            writes = [client.call(thread, "pexislink", name, {"entity_id": CAR})
                      for name in ("write_signal", "transmit_frame", "set_value", "execute")]
        for result in writes:
            self.assertEqual(body(result)["error"]["code"], "unknown_tool")
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        unix = f"socket.__new__ family={int(socket.AF_UNIX)}"
        lines = log.read_text().splitlines()
        self.assertTrue(lines, "the server-side audit log is empty: the hook did not run")
        unexpected = [line for line in lines if line != unix and not (
            line.startswith("open /dev/null ") and not int(line.split("flags=")[1].split()[0]) & write_flags)]
        self.assertEqual(unexpected, [])
        self.assertNoEgress(client)

    # C13 -------------------------------------------------------------------------------------------
    def test_C13_closing_codex_stops_every_server_process(self):
        marker = str(self.tmp / "lifecycle-marker.json")
        client = self.codex({"pexislink": server(GUEST, fixture_args=["--env-dump", marker])})
        thread = client.start_thread()
        self.assertFalse(client.call(thread, "pexislink", "list_entities", {})["isError"])
        self.assertTrue(children_with(marker), "the server process was not found while running")
        code, seconds = client.close()
        self.assertEqual(code, 0)
        self.assertLess(seconds, 10)
        for _ in range(50):
            if not children_with(marker):
                break
            import time
            time.sleep(0.1)
        self.assertEqual(children_with(marker), [], "server process left behind after Codex exited")


class CodexNotRunReport(unittest.TestCase):
    def test_report_whether_codex_ran(self):
        if not (HAVE_CODEX and HAVE_MCP):
            print("\n[compat] Codex CLI interoperability: not_run (PEXISLINK_CODEX_BIN unset or MCP SDK missing)",
                  file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
