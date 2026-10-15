"""Bounded, per-entity, in-process observation memory.

Retention: at most ``max_per_entity`` observations per entity, oldest evicted
first (FIFO by arrival). Nothing is written to disk; the memory dies with the
process. Each entity has its own buffer: no read path can return another
entity's observations. Ingest re-derives evidence quality: an adapter can claim
``observed`` or ``unknown`` but never ``late``, and the core downgrades
``observed`` when the data is late, out of order or out of range.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import ContractError, EntitySpec, EvidenceState, Observation, parse_observation

FUTURE_TOLERANCE_US = 5_000_000


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    code: str            # stored | <rejection code>
    seq: int | None = None
    status: str | None = None


class _EntityBuffer:
    def __init__(self, spec: EntitySpec, capacity: int) -> None:
        self.spec = spec
        self.items: deque[tuple[int, Observation]] = deque(maxlen=capacity)
        self.event_ids: set[str] = set()
        self.next_seq = 1
        self.evicted = 0
        self.last_observed_at: int | None = None
        self.rejected: Counter[str] = Counter()


class ObservationStore:
    def __init__(self, clock: Callable[[], int], max_entities: int = 256, max_per_entity: int = 10_000,
                 max_depth: int = 4, max_string: int = 64, max_keys: int = 32) -> None:
        self._clock = clock
        self._max_entities = max_entities
        self._max_per_entity = max_per_entity
        self._parse_limits = {"max_depth": max_depth, "max_string": max_string, "max_keys": max_keys}
        self._buffers: dict[str, _EntityBuffer] = {}
        self._lock = threading.Lock()
        self.unattributed_rejections: Counter[str] = Counter()

    def register(self, spec: EntitySpec) -> None:
        if not isinstance(spec, EntitySpec):
            raise ContractError("invalid_entity", "entity")
        with self._lock:
            if spec.entity_id in self._buffers:
                raise ContractError("duplicate_entity", "entity_id")
            if len(self._buffers) >= self._max_entities:
                raise ContractError("too_many_entities", "entities")
            self._buffers[spec.entity_id] = _EntityBuffer(spec, self._max_per_entity)

    def spec(self, entity_id: str) -> EntitySpec | None:
        buffer = self._buffers.get(entity_id) if isinstance(entity_id, str) else None
        return buffer.spec if buffer else None

    def entity_ids(self) -> list[str]:
        return sorted(self._buffers)

    def ingest(self, raw: object) -> IngestResult:
        try:
            obs = parse_observation(raw, **self._parse_limits)
        except ContractError as error:
            with self._lock:
                self.unattributed_rejections[error.code] += 1
            return IngestResult(False, error.code)
        with self._lock:
            buffer = self._buffers.get(obs["entity_id"])
            if buffer is None:
                self.unattributed_rejections["unknown_entity"] += 1
                return IngestResult(False, "unknown_entity")
            code = self._semantic_rejection(buffer, obs)
            if code is not None:
                buffer.rejected[code] += 1
                return IngestResult(False, code)
            status, flags = self._quality(buffer, obs)
            seq = buffer.next_seq
            buffer.next_seq += 1
            if len(buffer.items) == buffer.items.maxlen:
                _, oldest = buffer.items[0]
                buffer.event_ids.discard(oldest.event_id)
                buffer.evicted += 1
            buffer.items.append((seq, Observation(
                obs["event_id"], obs["entity_id"], obs["observed_at"], obs["received_at"], obs["source"],
                obs["kind"], tuple(sorted(obs["payload"].items())), status, flags,
                tuple(sorted(obs["provenance"].items())))))
            buffer.event_ids.add(obs["event_id"])
            if buffer.last_observed_at is None or obs["observed_at"] > buffer.last_observed_at:
                buffer.last_observed_at = obs["observed_at"]
            return IngestResult(True, "stored", seq, status)

    def _semantic_rejection(self, buffer: _EntityBuffer, obs: dict) -> str | None:
        spec = buffer.spec
        if obs["source"] != spec.adapter:
            return "source_mismatch"  # an adapter cannot write observations for another adapter's entity
        if obs["event_id"] in buffer.event_ids:
            return "duplicate_event"
        if obs["received_at"] > self._clock() + FUTURE_TOLERANCE_US:
            return "received_in_future"
        for name, value in obs["payload"].items():
            signal = spec.signal(name)
            if signal is None:
                return "undeclared_signal"
            if not signal.accepts_type(value):
                return "signal_type_mismatch"
        return None

    @staticmethod
    def _quality(buffer: _EntityBuffer, obs: dict) -> tuple[str, tuple[str, ...]]:
        spec, flags = buffer.spec, set(obs["flags"])
        status = obs["status"]
        if any(not spec.signal(n).in_range(v) for n, v in obs["payload"].items()):
            flags.add("out_of_range")
        if flags:  # out_of_range, unknown_frame, length_mismatch: no trustworthy measurement
            status = EvidenceState.UNKNOWN.value
        if obs["received_at"] - obs["observed_at"] > spec.max_latency_s * 1_000_000:
            flags.add("late_arrival")
        if buffer.last_observed_at is not None and obs["observed_at"] < buffer.last_observed_at:
            flags.add("out_of_order")
        if status == EvidenceState.OBSERVED.value and flags:
            status = EvidenceState.LATE.value
        return status, tuple(sorted(flags))

    # --- reads (callers must have passed the policy check) ---

    def page(self, entity_id: str, after_seq: int, limit: int) -> tuple[list[tuple[int, Observation]], bool]:
        with self._lock:
            buffer = self._buffers[entity_id]
            items = [item for item in buffer.items if item[0] > after_seq]
        return items[:limit], len(items) > limit

    def window(self, entity_id: str, size: int) -> list[Observation]:
        with self._lock:
            items = list(self._buffers[entity_id].items)[-size:]
        return [obs for _, obs in items]

    def latest(self, entity_id: str) -> tuple[int, Observation] | None:
        """The observation with the greatest observed_at (ties: latest arrival)."""
        with self._lock:
            items = list(self._buffers[entity_id].items)
        if not items:
            return None
        return max(items, key=lambda item: (item[1].observed_at, item[0]))

    def stats(self, entity_id: str) -> dict:
        with self._lock:
            buffer = self._buffers[entity_id]
            statuses = Counter(obs.status for _, obs in buffer.items)
            return {
                "stored": len(buffer.items), "capacity": buffer.items.maxlen, "evicted": buffer.evicted,
                "oldest_seq": buffer.items[0][0] if buffer.items else None,
                "newest_seq": buffer.items[-1][0] if buffer.items else None,
                "status_counts": {s: statuses.get(s, 0) for s in ("observed", "late", "unknown")},
                "rejected": dict(sorted(buffer.rejected.items())),
            }
