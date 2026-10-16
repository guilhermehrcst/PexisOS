"""Test-only stdio launcher for the compatibility suite. NOT part of PexisLink.

Serves the same synthetic demo world as ``pexislink fabric mcp-stdio`` through
the same ``pexislink.fabric.mcp_server.serve_stdio``, with operator-side knobs
that a test needs and the production CLI deliberately does not expose:

  --revoke-after N GRANT   revoke GRANT (operator action) after N tool calls
  --max-result-bytes N     run with a smaller FabricLimits.max_result_bytes
  --audit-log FILE         record security-relevant CPython audit events
                           (network, process, native loading, device opens)
  --env-dump FILE          write the sorted environment variable NAMES (never values)

Authorization still flows only through Session.call; nothing here grants access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

WATCHED = ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
           "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "ctypes.dlopen")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--principal", required=True)
    parser.add_argument("--revoke-after", nargs=2, metavar=("N", "GRANT"))
    parser.add_argument("--max-result-bytes", type=int)
    parser.add_argument("--audit-log")
    parser.add_argument("--env-dump")
    args = parser.parse_args()

    if args.env_dump:
        with open(args.env_dump, "w", encoding="utf-8") as handle:
            json.dump(sorted(os.environ), handle)
    if args.audit_log:
        log = open(args.audit_log, "a", encoding="utf-8", buffering=1)  # opened before the hook

        def hook(event: str, hook_args: tuple) -> None:
            if event.startswith(WATCHED) or (event == "open" and isinstance(hook_args[0], str)
                                              and hook_args[0].startswith(("/dev/", "/sys/class/net"))):
                if event == "open":
                    log.write(f"open {hook_args[0]} flags={hook_args[2]} mode={hook_args[1]}\n")
                else:
                    log.write(event + "\n")

        sys.addaudithook(hook)

    import anyio

    from pexislink.fabric import Fabric, FabricLimits, ManualClock
    from pexislink.fabric.adapters import appliance, service, vehicle
    from pexislink.fabric.demo import DEMO_NOW, demo_grants
    from pexislink.fabric.mcp_server import serve_stdio

    limits = FabricLimits(max_result_bytes=args.max_result_bytes) if args.max_result_bytes else FabricLimits()
    fabric = Fabric(clock=ManualClock(DEMO_NOW), limits=limits)
    for module, raws in ((appliance, appliance.generate()), (vehicle, vehicle.generate()),
                         (service, service.generate())):
        fabric.register(module.entity())
        fabric.ingest_all(raws)
    for grant in demo_grants():
        fabric.policy.add_grant(grant)
    session = fabric.open_session(args.principal)

    if args.revoke_after:
        limit, grant_id = int(args.revoke_after[0]), args.revoke_after[1]
        original, calls = session.call, [0]

        def call(tool, arguments):
            calls[0] += 1
            if calls[0] == limit + 1:
                fabric.policy.revoke(grant_id)  # operator action between two client calls
            return original(tool, arguments)

        session.call = call  # the transport still reaches the policy only through Session.call

    anyio.run(serve_stdio, session)


if __name__ == "__main__":
    main()
