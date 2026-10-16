"""Connect the official MCP Python SDK client to the Agent Fabric server over stdio.

    pip install --require-hashes -r requirements/fabric-mcp.txt
    PYTHONPATH=src python examples/fabric/mcp_client.py [demo:principal:guest]

The client launches ``pexislink fabric mcp-stdio`` as a child process, exactly
as an MCP host would from its configuration. The principal is chosen by
whoever launches the server, never by the model or the tool arguments.
"""

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

PRINCIPAL = sys.argv[1] if len(sys.argv) > 1 else "demo:principal:analyst"
SRC = str(Path(__file__).resolve().parents[2] / "src")


async def main() -> None:
    server = StdioServerParameters(command=sys.executable,
                                   args=["-m", "pexislink", "fabric", "mcp-stdio", "--principal", PRINCIPAL],
                                   env={"PYTHONPATH": SRC, "PATH": os.environ.get("PATH", "")})
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        print("tools:", sorted(t.name for t in tools))
        listing = await client.call_tool("list_entities", {})
        print("entities:", [e["entity_id"] for e in listing.structured_content["entities"]])
        for entity in [e["entity_id"] for e in listing.structured_content["entities"]]:
            result = await client.call_tool("explain_assessment", {"entity_id": entity})
            if result.is_error:
                print(entity, "->", json.loads(result.content[0].text)["error"])
            else:
                print(entity, "->", result.structured_content["state"], result.structured_content["confidence"])
        spoof = await client.call_tool("list_entities", {"principal": "demo:principal:analyst"})
        print("spoofed principal argument ->", json.loads(spoof.content[0].text)["error"]["code"])


anyio.run(main)
