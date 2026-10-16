"""Deterministic developer demo world: three synthetic entities, two principals.

Everything is in memory and generated from fixed seeds; the clock is fixed at
``DEMO_NOW`` so every run (and every Python version) yields identical output.
"""

from __future__ import annotations

from .adapters import appliance, service, vehicle
from .adapters._common import T0
from .contracts import canonical_json
from .policy import Grant, ManualClock
from .runtime import Fabric, FabricError

DEMO_NOW = T0 + 4 * 3600 * 1_000_000          # 2026-01-01T04:00:00Z
DAY = 86_400 * 1_000_000
ANALYST = "demo:principal:analyst"
GUEST = "demo:principal:guest"
ALL = frozenset({"observe", "describe", "read_history", "assess"})


def demo_grants() -> list[Grant]:
    return [
        Grant("demo:grant:analyst-fridge", ANALYST, appliance.ENTITY_ID, ALL, T0 - DAY, T0 + 30 * DAY),
        Grant("demo:grant:analyst-service", ANALYST, service.ENTITY_ID,
              frozenset({"describe", "observe", "assess"}), T0 - DAY, T0 + 30 * DAY),
        Grant("demo:grant:analyst-vehicle", ANALYST, vehicle.ENTITY_ID, ALL, T0 - DAY, T0 + 30 * DAY),
        Grant("demo:grant:analyst-vehicle-old", ANALYST, vehicle.ENTITY_ID, ALL, T0 - 10 * DAY, T0 - DAY),
        Grant("demo:grant:guest-fridge", GUEST, appliance.ENTITY_ID, frozenset({"describe", "observe"}),
              T0 - DAY, T0 + 30 * DAY),
    ]


def build_demo_fabric(grants: list[Grant] | None = None) -> tuple[Fabric, dict]:
    fabric = Fabric(clock=ManualClock(DEMO_NOW))
    ingest = {}
    for module, raws in ((appliance, appliance.generate()), (vehicle, vehicle.generate()),
                         (service, service.generate())):
        fabric.register(module.entity())
        ingest[module.ENTITY_ID] = fabric.ingest_all(raws)
    for grant in demo_grants() if grants is None else grants:
        fabric.policy.add_grant(grant)
    return fabric, ingest


def run_demo() -> dict:
    """The scripted walk-through printed by ``pexislink fabric demo``."""
    fabric, ingest = build_demo_fabric()
    analyst, guest = fabric.open_session(ANALYST), fabric.open_session(GUEST)

    def attempt(session, tool, args):
        try:
            return session.call(tool, args)
        except FabricError as error:
            return error.to_dict()

    def brief(assessment):
        return {"state": assessment.get("state"), "confidence": assessment.get("confidence"),
                "signals": {s["signal"]: s["state"] for s in assessment.get("signals", [])}}

    steps = {
        "ingest": ingest,
        "analyst.list_entities": attempt(analyst, "list_entities", {}),
        "guest.list_entities": attempt(guest, "list_entities", {}),
        "analyst.assessments": {e: brief(attempt(analyst, "explain_assessment", {"entity_id": e}))
                                for e in (appliance.ENTITY_ID, vehicle.ENTITY_ID, service.ENTITY_ID)},
        "analyst.latest_service": attempt(analyst, "read_observations",
                                          {"entity_id": service.ENTITY_ID, "mode": "latest"})["freshness"],
        "analyst.service_history_denied": attempt(analyst, "read_observations",
                                                  {"entity_id": service.ENTITY_ID, "mode": "history"}),
        "guest.vehicle_hidden": attempt(guest, "describe_entity", {"entity_id": vehicle.ENTITY_ID}),
        "guest.nonexistent": attempt(guest, "describe_entity", {"entity_id": "demo:vehicle:does-not-exist"}),
        "guest.fridge_assess_denied": attempt(guest, "explain_assessment", {"entity_id": appliance.ENTITY_ID}),
        "spoofed_principal_argument": attempt(guest, "list_entities", {"principal": ANALYST}),
        "write_tool_absent": attempt(analyst, "transmit", {"entity_id": vehicle.ENTITY_ID}),
        "analyst.capabilities_vehicle": attempt(analyst, "list_capabilities", {"entity_id": vehicle.ENTITY_ID}),
    }
    fabric.policy.revoke("demo:grant:analyst-fridge")
    steps["after_revocation.analyst_fridge"] = attempt(analyst, "explain_assessment",
                                                      {"entity_id": appliance.ENTITY_ID})
    records = fabric.audit.records()
    steps["audit"] = {"records": len(records), "chain_valid": fabric.audit.verify(records),
                      "executed": sum(1 for r in records if r["executed"]),
                      "denied": sum(1 for r in records if r["decision"] == "deny")}
    return steps


def demo_json() -> str:
    return canonical_json(run_demo())
