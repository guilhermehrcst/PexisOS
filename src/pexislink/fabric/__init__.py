"""PexisLink Agent Fabric v0.1 (experimental): a read-only, policy-gated,
evidence-qualified observation layer for agents.

Standard library only. The optional MCP transport lives in
``pexislink.fabric.mcp_server`` and is the only module that imports the MCP SDK
(``pip install 'pexislink[mcp]'``); nothing here imports it.
"""

from .assess import AssessmentConfig
from .contracts import (
    OBSERVATION_SCHEMA,
    Capability,
    ContractError,
    EntitySpec,
    EvidenceState,
    Observation,
    SignalSpec,
)
from .policy import AuditLog, Grant, ManualClock, PolicyEngine, Principal, load_policy_file
from .runtime import FABRIC_VERSION, Fabric, FabricError, FabricLimits, Session

__all__ = [
    "AssessmentConfig", "AuditLog", "Capability", "ContractError", "EntitySpec", "EvidenceState", "FABRIC_VERSION",
    "Fabric", "FabricError", "FabricLimits", "Grant", "ManualClock", "OBSERVATION_SCHEMA", "Observation",
    "PolicyEngine", "Principal", "Session", "SignalSpec", "load_policy_file",
]
