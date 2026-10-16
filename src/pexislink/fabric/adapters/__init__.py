"""Synthetic adapters. Each one turns domain-specific synthetic data into the
same core observation contract. Domain knowledge lives here, never in the core.

No adapter opens a device, a socket, a bus interface or a file: every value is
generated deterministically from a seed. Identifiers are synthetic
(``demo:...``); no VINs, serial numbers or personal identifiers are used.
"""

from . import appliance, service, vehicle

ADAPTERS = {appliance.ADAPTER_ID: appliance, vehicle.ADAPTER_ID: vehicle, service.ADAPTER_ID: service}

__all__ = ["ADAPTERS", "appliance", "service", "vehicle"]
