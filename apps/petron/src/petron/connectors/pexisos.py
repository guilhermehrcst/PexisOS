"""Optional, read-only client of the PexisOS Agent Fabric over MCP stdio (M3).

Contract consumed (PexisOS ``main`` @ db097c8, Agent Fabric v0.1, experimental):
``pexislink fabric mcp-stdio --principal ID`` serving five read-only tools over a deterministic
**synthetic** demo world. PETRON adds nothing to PexisOS; PexisOS never imports PETRON.

Hardening on the client side (independent of the server's own checks):

* the principal comes from the operator's mission spec, never from a plan or a model;
* the server is launched with ``-I -B`` (isolated mode: no environment variables, no user site,
  no current-directory imports, no bytecode writes), a minimal environment, an empty working
  directory, and an explicit source path chosen by the operator;
* the server's identity and tool list are checked after ``initialize``: any tool outside the
  five known read-only tools, or a tool not annotated read-only, makes the connector refuse to
  work at all (fail closed);
* only allowlisted tool names can be called; results are size-bounded; errors keep codes only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT = "pexislink-agent-fabric/0.1"
SERVER_NAME = "pexislink-agent-fabric"
READ_ONLY_TOOLS = frozenset({"list_entities", "describe_entity", "list_capabilities", "read_observations",
                             "explain_assessment"})
MAX_RESULT_BYTES = 256 * 1024
_LAUNCH = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
           "sys.argv = ['pexislink'] + sys.argv[2:]; runpy.run_module('pexislink', run_name='__main__')")


class ConnectorError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FabricConfig:
    principal: str
    pexislink_src: Path            # PexisOS ``src`` directory, read-only use
    python: str = sys.executable
    timeout_s: float = 30.0


def _find_connector_error(error: BaseException) -> ConnectorError | None:
    """Our own refusal, wherever task groups nested it."""
    if isinstance(error, ConnectorError):
        return error
    for inner in getattr(error, "exceptions", ()) or ():
        found = _find_connector_error(inner)
        if found is not None:
            return found
    return None


class FabricConnector:
    simulated = False

    def __init__(self, config: FabricConfig):
        if not (Path(config.pexislink_src) / "pexislink" / "__init__.py").is_file():
            raise ConnectorError("pexislink_src_invalid")
        self.config = config

    def _params(self, cwd: str):
        from mcp.client.stdio import StdioServerParameters
        return StdioServerParameters(
            command=self.config.python,
            args=["-I", "-B", "-c", _LAUNCH, str(Path(self.config.pexislink_src).resolve()),
                  "fabric", "mcp-stdio", "--principal", self.config.principal],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, cwd=cwd)

    async def _session_run(self, calls: list[tuple[str, dict]]) -> dict:
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="petron-fabric-") as cwd, open(os.devnull, "w") as errlog:
            with anyio.fail_after(self.config.timeout_s):
                async with stdio_client(self._params(cwd), errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        server = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                        if getattr(server, "name", None) != SERVER_NAME:
                            raise ConnectorError("unexpected_server")
                        listed = (await session.list_tools()).tools
                        names = {tool.name for tool in listed}
                        if names != READ_ONLY_TOOLS:
                            raise ConnectorError("unexpected_tool_set")
                        for tool in listed:
                            annotations = getattr(tool, "annotations", None)
                            if not annotations or getattr(annotations, "read_only_hint", None) is not True:
                                raise ConnectorError("tool_not_read_only")
                        results = []
                        for name, args in calls:
                            results.append(self._decode(await session.call_tool(name, args)))
                        return {"server": {"name": server.name, "version": getattr(server, "version", None)},
                                "tools": sorted(names), "results": results}

    @staticmethod
    def _decode(result: Any) -> dict:
        structured = getattr(result, "structured_content", None)
        if structured is None:
            texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", None) == "text"]
            try:
                structured = json.loads(texts[0]) if texts else None
            except ValueError:
                raise ConnectorError("undecodable_result") from None
        if len(json.dumps(structured)) > MAX_RESULT_BYTES:
            raise ConnectorError("result_too_large")
        if getattr(result, "is_error", False):
            error = (structured or {}).get("error", {}) if isinstance(structured, dict) else {}
            return {"is_error": True, "code": str(error.get("code", "unknown"))[:64],
                    "reason": str(error.get("reason", ""))[:64] or None}
        return {"is_error": False, "data": structured}

    def run(self, calls: list[tuple[str, dict]]) -> dict:
        for name, args in calls:
            if name not in READ_ONLY_TOOLS:
                raise ConnectorError("tool_not_allowlisted")
            if not isinstance(args, dict):
                raise ConnectorError("invalid_arguments")
        try:
            import anyio
        except ImportError:
            raise ConnectorError("mcp_unavailable") from None
        try:
            return anyio.run(self._session_run, calls)
        except ConnectorError:
            raise
        except BaseException as error:  # noqa: BLE001 - includes anyio exception groups and timeouts
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            found = _find_connector_error(error)
            if found is not None:
                raise found from None
            raise ConnectorError(f"transport_error:{type(error).__name__}") from None

    def call(self, name: str, args: dict) -> dict:
        outcome = self.run([(name, args)])["results"][0]
        if outcome["is_error"]:
            raise ConnectorError(f"fabric_error:{outcome['code']}")
        return outcome["data"]


class SimulatedFabricConnector:
    """Stand-in used only when PexisOS is unreachable. Its data is labelled simulated and must
    never be reported as an integration result."""

    simulated = True

    def __init__(self, principal: str):
        self.principal = principal

    def call(self, name: str, args: dict) -> dict:
        if name not in READ_ONLY_TOOLS:
            raise ConnectorError("tool_not_allowlisted")
        return {"simulated": True, "tool": name, "note": "simulated contract response, not PexisOS data"}
