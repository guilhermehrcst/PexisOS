"""Helpers shared by the synthetic adapters (no I/O)."""

from __future__ import annotations

import math
import random
from typing import Any

from ..contracts import OBSERVATION_SCHEMA, format_time

T0 = 1_767_225_600_000_000  # 2026-01-01T00:00:00Z in microseconds


class Noise:
    """Deterministic Gaussian noise from ``random.Random.random`` only (Box-Muller),
    so values do not depend on the Python version's ``gauss`` implementation."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def uniform(self) -> float:
        return self.rng.random()

    def gauss(self, sigma: float) -> float:
        u1 = max(self.rng.random(), 1e-12)
        u2 = self.rng.random()
        return sigma * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def observation(*, event_id: str, entity_id: str, adapter: str, kind: str, observed_at: int, received_at: int,
                payload: dict[str, Any], generator: str, seed: int, status: str = "observed",
                flags: tuple[str, ...] = ()) -> dict:
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "event_id": event_id,
        "entity_id": entity_id,
        "observed_at": format_time(observed_at),
        "received_at": format_time(received_at),
        "source": adapter,
        "kind": kind,
        "payload": payload,
        "quality": {"status": status, "flags": list(flags)},
        "provenance": {"source": "synthetic_fixture", "license": "project_original", "generator": generator,
                       "seed": seed},
    }


def arrival_order(observations: list[dict]) -> list[dict]:
    """Deliver in order of reception (late observations arrive after newer ones)."""
    return sorted(observations, key=lambda o: (o["received_at"], o["event_id"]))
