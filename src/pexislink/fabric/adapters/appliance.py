"""Synthetic refrigerator: temperature, compressor state and cycles.

Scenario (deterministic per seed): the compressor cycles between 3 and 5 degC;
some readings arrive late, a block of readings is missing, one reading is a
sensor glitch outside the plausible range, and during the last 25 minutes the
cooling becomes weak so the temperature climbs (unusual variation).
"""

from __future__ import annotations

from ..contracts import Capability, EntitySpec, SignalSpec
from ._common import T0, Noise, arrival_order, observation

ADAPTER_ID = "synthetic:appliance-v0"
GENERATOR = "pexislink.fabric.adapters.appliance"
ENTITY_ID = "demo:appliance:fridge-01"
INTERVAL_S = 60


def entity(entity_id: str = ENTITY_ID) -> EntitySpec:
    return EntitySpec(
        entity_id=entity_id, kind="appliance.refrigerator", display_name="Synthetic refrigerator 01",
        adapter=ADAPTER_ID, capabilities=frozenset(c.value for c in Capability),
        signals=(SignalSpec("temperature_c", "number", "degC", -40.0, 40.0),
                 SignalSpec("compressor_on", "boolean"),
                 SignalSpec("cycle_count", "integer", None, 0, 10_000_000, assessable=False)),
        expected_interval_s=INTERVAL_S, max_latency_s=120, stale_after_s=600)


def generate(seed: int = 1, count: int = 240, start: int = T0, entity_id: str = ENTITY_ID) -> list[dict]:
    noise = Noise(seed)
    temperature, on, cycles = 4.0, False, 0
    out = []
    for i in range(count):
        weak = i >= count - 25  # failing door seal: the compressor runs but cannot hold the temperature
        if on:
            temperature += 0.25 if weak else -0.12
            if temperature <= 3.0:
                on = False
        else:
            temperature += 0.30 if weak else 0.05
            if temperature >= 5.0:
                on, cycles = True, cycles + 1
        if 100 <= i < 110:
            continue  # missing block: the sensor did not report
        observed_at = start + i * INTERVAL_S * 1_000_000
        delay_s = 400 if i % 37 == 5 else 1 + int(noise.uniform() * 3)
        reading = round(temperature + noise.gauss(0.05), 2)
        if i == 150:
            reading = 120.0  # glitch: outside the declared plausible range
        out.append(observation(event_id=f"fridge-01-{seed}-{i:05d}", entity_id=entity_id, adapter=ADAPTER_ID,
                               kind="appliance.telemetry", observed_at=observed_at,
                               received_at=observed_at + delay_s * 1_000_000,
                               payload={"temperature_c": reading, "compressor_on": on, "cycle_count": cycles},
                               generator=GENERATOR, seed=seed))
    return arrival_order(out)
