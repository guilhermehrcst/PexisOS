"""PexisOS connector tests.

* Contract tests against the real Agent Fabric run only when ``mcp`` is importable and a PexisOS
  ``src`` directory is available (``PETRON_PEXISLINK_SRC``); otherwise they are skipped.
* Hostile-server tests launch a fake ``pexislink`` package that lies about its tools; the
  connector must refuse to work with it.
"""

import importlib.util
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from petron.connectors.pexisos import (ConnectorError, FabricConfig, FabricConnector, SimulatedFabricConnector,
                                       digest_source)
from petron.mission import MissionState
from petron.tools import Gateway, MissionContext
from petron.workspace import Workspace

from support import EngineCase

HAVE_MCP = importlib.util.find_spec("mcp") is not None
# In the PexisOS monorepo the Agent Fabric lives at <repo>/src; PETRON_PEXISLINK_SRC overrides it.
_MONOREPO_SRC = Path(__file__).resolve().parents[3] / "src"
PEXISLINK_SRC = os.environ.get("PETRON_PEXISLINK_SRC") or (
    str(_MONOREPO_SRC) if (_MONOREPO_SRC / "pexislink" / "fabric").is_dir() else "")
HAVE_FABRIC = HAVE_MCP and bool(PEXISLINK_SRC) and (Path(PEXISLINK_SRC) / "pexislink" / "fabric").is_dir()
ANALYST, GUEST = "demo:principal:analyst", "demo:principal:guest"
VEHICLE = "demo:vehicle:synthetic-01"

_FAKE_SERVER = textwrap.dedent('''
    import json, pathlib, anyio
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    MODE = (pathlib.Path(__file__).parent / "mode.txt").read_text().strip()
    names = ["list_entities", "describe_entity", "list_capabilities", "read_observations", "explain_assessment"]
    if MODE == "extra_tool":
        names.append("transmit_can")
    tools = [types.Tool(name=n, description="x",
                        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                        annotations=types.ToolAnnotations(read_only_hint=(MODE != "not_read_only")))
             for n in names]
    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=tools)
    async def call_tool(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"ok": True}))])
    server = Server("impostor" if MODE == "wrong_name" else "pexislink-agent-fabric", version="0",
                    on_list_tools=list_tools, on_call_tool=call_tool)
    async def main():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    anyio.run(main)
''')


class AllowlistTests(unittest.TestCase):
    def test_non_allowlisted_tool_refused_before_any_process_starts(self):
        connector = FabricConnector.__new__(FabricConnector)  # no config needed: refused up front
        with self.assertRaises(ConnectorError) as caught:
            connector.run([("transmit_can", {})])
        self.assertEqual(caught.exception.code, "tool_not_allowlisted")

    def test_invalid_source_refused(self):
        with self.assertRaises(ConnectorError):
            FabricConnector(FabricConfig(ANALYST, Path(tempfile.gettempdir()) / "no-such-pexislink"))

    def test_simulated_connector_labels_its_data(self):
        self.assertTrue(SimulatedFabricConnector(ANALYST).call("list_entities", {})["simulated"])


@unittest.skipUnless(HAVE_MCP, "mcp not installed")
class HostileServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-fake-fabric-"))
        package = self.tmp / "pexislink"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text(_FAKE_SERVER)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refused(self, mode):
        (self.tmp / "pexislink" / "mode.txt").write_text(mode)
        with self.assertRaises(ConnectorError) as caught:
            FabricConnector(FabricConfig(ANALYST, self.tmp, timeout_s=20,
                                         pexislink_src_sha256=digest_source(self.tmp))).run([("list_entities", {})])
        return caught.exception.code

    def test_server_advertising_a_write_tool_is_refused(self):
        self.assertEqual(self.refused("extra_tool"), "unexpected_tool_set")

    def test_tool_not_annotated_read_only_is_refused(self):
        self.assertEqual(self.refused("not_read_only"), "tool_not_read_only")

    def test_impostor_server_name_is_refused(self):
        self.assertEqual(self.refused("wrong_name"), "unexpected_server")


@unittest.skipUnless(HAVE_FABRIC, "PexisOS Agent Fabric or mcp not available")
class FabricContractTests(unittest.TestCase):
    def connector(self, principal):
        return FabricConnector(FabricConfig(principal, Path(PEXISLINK_SRC),
                                            pexislink_src_sha256=digest_source(Path(PEXISLINK_SRC))))

    def test_analyst_sees_three_synthetic_entities(self):
        data = self.connector(ANALYST).call("list_entities", {})
        ids = sorted(entity["entity_id"] for entity in data["entities"])
        self.assertEqual(ids, ["demo:appliance:fridge-01", "demo:service:checkout-api", VEHICLE])

    def test_guest_cannot_see_ungranted_entity(self):
        outcome = self.connector(GUEST).run([("describe_entity", {"entity_id": VEHICLE})])["results"][0]
        self.assertTrue(outcome["is_error"])
        self.assertEqual(outcome["code"], "not_found")  # the Fabric hides ungranted entities entirely

    def test_principal_spoofing_argument_rejected_by_the_server_too(self):
        outcome = self.connector(GUEST).run([("list_entities", {"principal": ANALYST})])["results"][0]
        self.assertTrue(outcome["is_error"])
        self.assertEqual(outcome["code"], "invalid_arguments")

    def test_assessment_is_an_explained_inference(self):
        data = self.connector(ANALYST).call("explain_assessment", {"entity_id": VEHICLE})
        self.assertEqual(data["kind"], "inference")
        self.assertEqual(data["entity_id"], VEHICLE)
        self.assertIn(data["confidence"], {"low", "medium", "high", "unknown"})
        # Known PexisOS limit FAB-01: the stdio demo runs on a frozen clock.
        self.assertEqual(data["assessed_at"], "2026-01-01T04:00:00Z")


@unittest.skipUnless(HAVE_FABRIC, "PexisOS Agent Fabric or mcp not available")
class FabricThroughGatewayTests(EngineCase):
    def test_permitted_fabric_tool_runs_through_the_gateway(self):
        spec = self.spec()
        spec["permissions"]["tools"].append("pexisos.list_entities")
        spec["pexisos"] = {"principal": ANALYST}
        mission_id = self.engine.create(spec)
        plan_sha = self.engine.plan(mission_id)
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        self.engine.store.transition(mission_id, MissionState.AWAITING_APPROVAL, MissionState.RUNNING)
        ctx = MissionContext(self.engine.store, self.engine.store.spec(mission_id),
                             Workspace.create(self.source, self.tmp / "area", "work"), self.sandbox, plan_sha,
                             FabricConnector(FabricConfig(ANALYST, Path(PEXISLINK_SRC),
                                                          pexislink_src_sha256=digest_source(Path(PEXISLINK_SRC)))))
        outcome = Gateway().call(ctx, "pexisos.list_entities", {})
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(len(outcome["result"]["entities"]), 3)
        denied = Gateway().call(ctx, "pexisos.describe_entity", {"entity_id": VEHICLE})
        self.assertEqual(denied["error"], "tool_not_permitted")


if __name__ == "__main__":
    unittest.main()
