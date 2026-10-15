"""Shared builders for the Agent Fabric tests (synthetic data only)."""

from pexislink.fabric import Fabric, Grant, ManualClock
from pexislink.fabric.adapters import appliance, service, vehicle
from pexislink.fabric.adapters._common import T0, observation
from pexislink.fabric.demo import DEMO_NOW

DAY = 86_400 * 1_000_000
ALL = frozenset({"observe", "describe", "read_history", "assess"})
ALICE, BOB = "test:principal:alice", "test:principal:bob"
FRIDGE, CAR, API = appliance.ENTITY_ID, vehicle.ENTITY_ID, service.ENTITY_ID


def world(grants=(), now=DEMO_NOW, **fabric_kwargs):
    clock = ManualClock(now)
    fabric = Fabric(clock=clock, **fabric_kwargs)
    for module, raws in ((appliance, appliance.generate()), (vehicle, vehicle.generate()),
                         (service, service.generate())):
        fabric.register(module.entity())
        fabric.ingest_all(raws)
    for grant in grants:
        fabric.policy.add_grant(grant)
    return fabric, clock


def grant(grant_id, principal, entity, caps=ALL, not_before=T0 - DAY, expires_at=T0 + 30 * DAY):
    return Grant(grant_id, principal, entity, frozenset(caps), not_before, expires_at)


def fridge_obs(i, value=4.0, delay_s=1, **overrides):
    raw = observation(event_id=f"t-{i}", entity_id=FRIDGE, adapter=appliance.ADAPTER_ID, kind="appliance.telemetry",
                      observed_at=T0 + i * 60_000_000, received_at=T0 + i * 60_000_000 + delay_s * 1_000_000,
                      payload={"temperature_c": value}, generator="tests.fabric", seed=0)
    raw.update(overrides)
    return raw
