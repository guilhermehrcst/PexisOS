"""Synthetic software service: latency, availability, error rate, resources.

Scenario (deterministic per seed): steady behaviour with noise, two reporting
gaps, a latency rise near the end, and then the service stops reporting
entirely 40 minutes before the demo clock, so its latest data is stale.
"""

from __future__ import annotations

from ..contracts import Capability, EntitySpec, SignalSpec
from ._common import T0, Noise, arrival_order, observation

ADAPTER_ID = "synthetic:service-v0"
GENERATOR = "pexislink.fabric.adapters.service"
ENTITY_ID = "demo:service:checkout-api"
INTERVAL_S = 30


def entity(entity_id: str = ENTITY_ID) -> EntitySpec:
    return EntitySpec(
        entity_id=entity_id, kind="software.service", display_name="Synthetic checkout API",
        adapter=ADAPTER_ID, capabilities=frozenset(c.value for c in Capability),
        signals=(SignalSpec("latency_ms", "number", "ms", 0.0, 120_000.0),
                 SignalSpec("available", "boolean"),
                 SignalSpec("error_rate", "number", "ratio", 0.0, 1.0),
                 SignalSpec("cpu_pct", "number", "%", 0.0, 100.0),
                 SignalSpec("memory_pct", "number", "%", 0.0, 100.0),
                 SignalSpec("health", "enum", values=("up", "degraded", "down"))),
        expected_interval_s=INTERVAL_S, max_latency_s=60, stale_after_s=180)


def generate(seed: int = 3, count: int = 400, start: int = T0, entity_id: str = ENTITY_ID) -> list[dict]:
    noise = Noise(seed)
    out = []
    for i in range(count):
        if 120 <= i < 126 or 300 <= i < 304:
            continue  # reporting gaps
        rising = max(0, i - (count - 20))
        latency = max(1.0, 80.0 + noise.gauss(4.0) + rising * 25.0)
        errors = min(1.0, max(0.0, 0.002 + noise.gauss(0.001) + rising * 0.004))
        health = "up" if latency < 250 else "degraded"
        observed_at = start + i * INTERVAL_S * 1_000_000
        out.append(observation(event_id=f"checkout-api-{seed}-{i:05d}", entity_id=entity_id, adapter=ADAPTER_ID,
                               kind="service.health", observed_at=observed_at,
                               received_at=observed_at + (1 + int(noise.uniform() * 2)) * 1_000_000,
                               payload={"latency_ms": round(latency, 1), "available": True,
                                        "error_rate": round(errors, 4),
                                        "cpu_pct": round(min(100.0, max(0.0, 35 + noise.gauss(3.0))), 1),
                                        "memory_pct": round(min(100.0, max(0.0, 61 + noise.gauss(1.0))), 1),
                                        "health": health},
                               generator=GENERATOR, seed=seed))
    return arrival_order(out)
