"""Measure Agent Fabric v0.1 resource use on synthetic fixtures of known size.

    PYTHONPATH=src python scripts/fabric_measure.py [--per-entity 10000] [--memory]

Prints JSON: ingest throughput, per-call latency (median and max of repeated
calls), peak traced Python memory, and serialized result sizes. Numbers are
machine-dependent measurements, not guarantees; docs/AGENT-FABRIC.md records
one run with its environment.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import tracemalloc

from pexislink.fabric import Fabric, FabricLimits, Grant, ManualClock
from pexislink.fabric.adapters import appliance
from pexislink.fabric.adapters._common import T0, observation
from pexislink.fabric.contracts import canonical_json


def fixture(n: int) -> list[dict]:
    out = []
    for i in range(n):
        t = T0 + i * 60_000_000
        out.append(observation(event_id=f"m-{i}", entity_id=appliance.ENTITY_ID, adapter=appliance.ADAPTER_ID,
                               kind="appliance.telemetry", observed_at=t, received_at=t + 1_000_000,
                               payload={"temperature_c": 4.0 + (i % 7) * 0.1, "compressor_on": i % 2 == 0,
                                        "cycle_count": i // 30},
                               generator="scripts.fabric_measure", seed=0))
    return out


def timed(fn, repeat: int) -> dict:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 3), "max_ms": round(max(samples), 3), "runs": repeat}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-entity", type=int, default=10_000)
    parser.add_argument("--memory", action="store_true", help="trace memory (slows every timing down)")
    args = parser.parse_args()
    n = args.per_entity
    raws = fixture(n)
    now = T0 + n * 60_000_000
    if args.memory:
        tracemalloc.start()
    fabric = Fabric(clock=ManualClock(now), limits=FabricLimits(max_per_entity=n))
    fabric.register(appliance.entity())
    fabric.policy.add_grant(Grant("m:grant:1", "m:principal:1", appliance.ENTITY_ID,
                                  frozenset({"observe", "describe", "read_history", "assess"}), T0, now + 1))
    start = time.perf_counter()
    outcome = fabric.ingest_all(raws)
    ingest_s = time.perf_counter() - start
    _, peak_after_ingest = tracemalloc.get_traced_memory() if args.memory else (0, 0)
    session = fabric.open_session("m:principal:1")
    history = {"entity_id": appliance.ENTITY_ID, "mode": "history", "limit": 50}
    results = {
        "environment": {"python": sys.version.split()[0], "platform": platform.machine(),
                        "implementation": platform.python_implementation()},
        "fixture": {"entities": 1, "observations": n, "outcome": outcome},
        "ingest": {"seconds": round(ingest_s, 3), "observations_per_second": round(n / ingest_s)},
        "calls": {
            "read_observations_latest": timed(lambda: session.call(
                "read_observations", {"entity_id": appliance.ENTITY_ID, "mode": "latest"}), 50),
            "read_observations_history_page50": timed(lambda: session.call("read_observations", history), 50),
            "explain_assessment": timed(lambda: session.call(
                "explain_assessment", {"entity_id": appliance.ENTITY_ID}), 50),
            "list_entities": timed(lambda: session.call("list_entities", {}), 50),
        },
        "result_bytes": {
            "history_page50": len(canonical_json(session.call("read_observations", history))),
            "explain_assessment": len(canonical_json(session.call("explain_assessment",
                                                                  {"entity_id": appliance.ENTITY_ID}))),
        },
    }
    if not args.memory:
        print(json.dumps(results, indent=2, sort_keys=True))
        return
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results["memory"] = {"traced_peak_mib_after_ingest": round(peak_after_ingest / 2 ** 20, 2),
                         "traced_peak_mib_total": round(peak / 2 ** 20, 2),
                         "approx_bytes_per_stored_observation": round(peak_after_ingest / n)}
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
