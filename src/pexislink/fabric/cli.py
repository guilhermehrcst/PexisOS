"""``pexislink fabric``: developer demo and the optional stdio MCP server.

    pexislink fabric demo
    pexislink fabric mcp-stdio --principal ID [--policy FILE]

``mcp-stdio`` serves the deterministic synthetic demo world over MCP on
stdin/stdout for exactly one principal, chosen by whoever launches the process
(the operator's MCP client configuration). Grants come from the built-in demo
policy or, with ``--policy``, from a local policy file (closed schema, at most
64 KiB, no symlinks). There is no network transport.

Exit codes: 0 success; 2 usage or invalid policy/principal; 3 the optional MCP
dependency is not installed. Messages never contain paths or file contents.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Sequence

from .contracts import ContractError, check_id
from .demo import build_demo_fabric, run_demo
from .policy import load_policy_file


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="pexislink fabric", description="Agent Fabric v0.1 (experimental, read-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run the deterministic synthetic walk-through and print JSON")
    serve = sub.add_parser("mcp-stdio", help="serve the synthetic demo world over MCP (stdio) for one principal")
    serve.add_argument("--principal", required=True, help="principal bound to this server, e.g. demo:principal:analyst")
    serve.add_argument("--policy", help="local policy file (pexislink.policy.v0); default: built-in demo grants")
    args = parser.parse_args(list(argv))
    if args.command == "demo":
        print(json.dumps(run_demo(), indent=2, sort_keys=True))
        return 0
    try:
        check_id(args.principal, "principal")
        grants = load_policy_file(args.policy) if args.policy else None
    except ContractError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    if importlib.util.find_spec("mcp") is None:
        print("error: mcp_unavailable: install the optional dependency: pip install 'pexislink[mcp]'",
              file=sys.stderr)
        return 3
    import anyio

    from .mcp_server import serve_stdio

    try:
        fabric, _ = build_demo_fabric(grants)
    except ContractError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    session = fabric.open_session(args.principal)
    print(f"pexislink fabric: serving synthetic demo entities over MCP stdio (read-only); principal bound at launch",
          file=sys.stderr)
    anyio.run(serve_stdio, session)
    return 0
