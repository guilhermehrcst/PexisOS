"""Use the Agent Fabric from plain Python: no MCP, no LLM, no network.

    PYTHONPATH=src python examples/fabric/python_api.py
"""

from pexislink.fabric import Fabric, FabricError, Grant, ManualClock
from pexislink.fabric.adapters import appliance, service, vehicle
from pexislink.fabric.adapters._common import T0

HOUR = 3600 * 1_000_000
fabric = Fabric(clock=ManualClock(T0 + 4 * HOUR))           # deterministic clock

for adapter, raws in ((appliance, appliance.generate()), (vehicle, vehicle.generate()), (service, service.generate())):
    fabric.register(adapter.entity())                        # operator side: identity + declared capabilities
    print(adapter.ENTITY_ID, fabric.ingest_all(raws))        # untrusted adapter output is validated here

# Operator side: an expiring grant for one principal on one entity.
fabric.policy.add_grant(Grant("example:grant:1", "example:principal:ops", vehicle.ENTITY_ID,
                              frozenset({"describe", "observe", "assess"}), T0, T0 + 24 * HOUR))

# Request side: a session bound to that principal. No method takes a principal.
session = fabric.open_session("example:principal:ops")
assessment = session.call("explain_assessment", {"entity_id": vehicle.ENTITY_ID})
print(assessment["state"], {s["signal"]: s["state"] for s in assessment["signals"]})

try:
    session.call("read_observations", {"entity_id": vehicle.ENTITY_ID, "mode": "history"})
except FabricError as error:
    print(error.to_dict())                                   # read_history was not granted

try:
    session.call("describe_entity", {"entity_id": appliance.ENTITY_ID})
except FabricError as error:
    print(error.to_dict())                                   # no grant at all: indistinguishable from absent
