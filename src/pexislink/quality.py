"""Time and data-quality semantics, shared by the reference builder and detector.

Records are replayed in file (arrival) order. Each vehicle has O(1) state, so
memory is bounded by the number of vehicles, not the number of rows. Nothing is
reordered or dropped silently: every record leaves with an explicit integrity
state and issue codes.

Integrity states
    accepted               newer than the vehicle's last accepted record
    duplicate              same instant and identical content (excluded)
    conflicting_duplicate  same instant as the last accepted record, different
                           content (excluded; the detector also discards the
                           vehicle's evidence run)
    out_of_order           older than the last accepted record (excluded)
    unplaceable            no valid timestamp or vehicle id (excluded)

Quality issue codes (added to row-level codes from ``ingest``)
    <field>_implausible     outside the configured physical range
    coolant_temp_c_sentinel exactly an encoding-extreme value that loggers emit
                            on sensor faults (OBD-II PID 0x05: -40 C and 215 C)
    warmup_transient        within ``warmup_s`` of a segment start
    coolant_stuck_suspected the sensor-integrity state machine does not trust
                            this reading: after an engine-off gap of
                            ``stuck_gap_s`` it stayed within ``stuck_tolerance_c``
                            of the pre-gap value (provisional), or a suspicion
                            established after ``stuck_min_samples`` such readings
                            has not been cleared (see ``QualityTracker._stuck``).
                            Readings without a usable coolant value never carry
                            it; they are already unusable for another reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from .config import DetectorConfig
from .ingest import DEFAULT_MAX_VEHICLES, InputError, Record

Integrity = Literal["accepted", "duplicate", "conflicting_duplicate", "out_of_order", "unplaceable"]

_US = 1_000_000


@dataclass(frozen=True, slots=True)
class Annotated:
    record: Record
    integrity: Integrity
    issues: tuple[str, ...]
    segment_start: bool = False
    segment_elapsed_s: float | None = None
    warmup: bool = False
    coolant_plausible: bool = False  # accepted, parsed, in range, not a sentinel
    coolant_ok: bool = False         # coolant_plausible and not suspected stuck
    context_ok: bool = False         # rpm and speed parsed and plausible
    load_ok: bool = False            # load parsed and plausible (False if column absent)


@dataclass(frozen=True, slots=True)
class CoolantState:
    """An established frozen-sensor suspicion, carried from the reference file
    into the observations so that it does not end at the file boundary. Only
    SUSPECTED is carried: the time between two files is not known to be an
    engine-off period, so it must not start a new observation."""

    anchor: float
    anchored: int
    deviations: int


class _VehicleState:
    __slots__ = ("last_us", "last_content", "segment_start_us", "last_coolant", "last_coolant_us",
                 "anchor", "anchored", "stuck", "deviations")

    def __init__(self) -> None:
        self.last_us: int | None = None
        self.last_content: tuple[object, ...] = ()
        self.segment_start_us = 0
        self.last_coolant: float | None = None
        self.last_coolant_us: int | None = None
        self.anchor: float | None = None  # pre-gap reading the post-gap readings are compared with
        self.anchored = 0                 # consecutive post-gap readings within tolerance of it
        self.stuck = False
        self.deviations = 0               # consecutive readings outside tolerance while stuck


def _content(record: Record) -> tuple[object, ...]:
    return (record.engine_rpm, record.speed_kmh, record.coolant_temp_c,
            record.engine_load_pct, record.issues)


def _within(value: float | None, bounds: tuple[float, float]) -> bool:
    return value is not None and bounds[0] <= value <= bounds[1]


class QualityTracker:
    """Annotates one file's records. Use a fresh tracker per file."""

    def __init__(self, config: DetectorConfig, *, max_vehicles: int = DEFAULT_MAX_VEHICLES,
                 coolant_states: Mapping[str, CoolantState] | None = None) -> None:
        self.config = config
        self.max_vehicles = max_vehicles
        self._vehicles: dict[str, _VehicleState] = {}
        self._carried = coolant_states or {}
        self._segment_gap_us = round(config.segment_gap_s * _US)
        self._stuck_gap_us = round(config.stuck_gap_s * _US)

    def annotate(self, record: Record) -> Annotated:
        if record.epoch_us is None or record.vehicle_id is None:
            return Annotated(record, "unplaceable", record.issues)
        state = self._vehicles.get(record.vehicle_id)
        if state is None:
            if len(self._vehicles) >= self.max_vehicles:
                raise InputError(f"line {record.line}: more than {self.max_vehicles} vehicles")
            state = self._vehicles[record.vehicle_id] = _VehicleState()
            carried = self._carried.get(record.vehicle_id)
            if carried is not None:
                state.anchor, state.anchored, state.deviations = carried.anchor, carried.anchored, carried.deviations
                state.stuck = True

        now = record.epoch_us
        content = _content(record)
        if state.last_us is not None:
            if now < state.last_us:
                return Annotated(record, "out_of_order", record.issues)
            if now == state.last_us:
                integrity: Integrity = ("duplicate" if content == state.last_content
                                        else "conflicting_duplicate")
                if integrity == "conflicting_duplicate" and state.stuck:
                    state.deviations = 0  # a contradicted deviation is not recovery evidence
                return Annotated(record, integrity, record.issues)

        cfg = self.config
        segment_start = state.last_us is None or now - state.last_us > self._segment_gap_us
        if segment_start:
            state.segment_start_us = now
        state.last_us, state.last_content = now, content
        elapsed_s = (now - state.segment_start_us) / _US

        issues = list(record.issues)
        checks = ((record.coolant_temp_c, cfg.coolant_range_c, "coolant_temp_c"),
                  (record.engine_rpm, cfg.rpm_range, "engine_rpm"),
                  (record.speed_kmh, cfg.speed_range_kmh, "speed_kmh"),
                  (record.engine_load_pct, cfg.load_range_pct, "engine_load_pct"))
        for value, bounds, field in checks:
            if value is not None and not _within(value, bounds):
                issues.append(f"{field}_implausible")
        warmup = elapsed_s < cfg.warmup_s
        if warmup:
            issues.append("warmup_transient")

        coolant = record.coolant_temp_c
        plausible = coolant is not None and _within(coolant, cfg.coolant_range_c)
        if coolant is not None and coolant in cfg.coolant_sentinels_c:
            issues.append("coolant_temp_c_sentinel")
            plausible = False
        stuck = False
        if plausible and coolant is not None:
            stuck = self._stuck(state, coolant, now)
            if stuck:
                issues.append("coolant_stuck_suspected")
        elif state.stuck:
            state.deviations = 0  # no usable reading: recovery evidence must be consecutive

        return Annotated(
            record, "accepted", tuple(issues), segment_start, elapsed_s, warmup,
            coolant_plausible=plausible,
            coolant_ok=plausible and not stuck,
            context_ok=(_within(record.engine_rpm, cfg.rpm_range)
                        and _within(record.speed_kmh, cfg.speed_range_kmh)),
            load_ok=_within(record.engine_load_pct, cfg.load_range_pct),
        )

    def coolant_states(self) -> dict[str, CoolantState]:
        """Established suspicions after the last record, per vehicle."""
        return {vehicle: CoolantState(state.anchor, state.anchored, state.deviations)
                for vehicle, state in self._vehicles.items() if state.stuck and state.anchor is not None}

    def _stuck(self, state: _VehicleState, value: float, now: int) -> bool:
        """Physics-based stuck-sensor suspicion; returns True if this reading is suspect.

        After an engine-off gap of at least ``stuck_gap_s`` a real coolant reading
        must have dropped (the engine cooled while parked) and must then rise
        during warm-up. Readings that stay within ``stuck_tolerance_c`` (1 LSB by
        default, so a sensor dithering between two codes is covered) of the
        pre-gap value for ``stuck_min_samples`` samples are suspected stuck.

        State machine, per vehicle, over accepted readings (excluded records
        never reach it, except that a conflicting duplicate restarts the
        recovery count in SUSPECTED):
            HEALTHY    no anchor; readings are trusted. A plausible reading after
                       a gap >= stuck_gap_s since the last plausible reading
                       moves to OBSERVING with anchor = that last reading.
            OBSERVING  within tolerance: suspect (provisional) and counted; at
                       stuck_min_samples move to SUSPECTED. Outside tolerance:
                       back to HEALTHY, and that reading is trusted (the sensor
                       moved after the park). A logger's single cached start-up
                       value therefore costs one abstention, never more.
            SUSPECTED  every plausible reading is suspect until recovery. Within
                       tolerance, an accepted record without a usable coolant
                       value (missing, implausible, sentinel) or a conflicting
                       duplicate restarts the recovery count. Recovery needs two
                       consecutive plausible readings outside tolerance, in any
                       direction: the first is still suspect, the second clears
                       the state and is the first trusted one. Time between them
                       does not matter: a working sensor reads the cooled engine
                       after a park.
        Invariant: the return value is True whenever the reading is within
        tolerance of an active anchor or the state is SUSPECTED after it, so a
        reading whose integrity is unresolved is never handed to the relative
        barrier and can never produce "normal". The cost is abstention, never a
        false alarm, and the absolute barrier still evaluates suspect readings.
        An established suspicion is carried from the reference file into the
        observations (``coolant_states``); nothing else crosses the boundary. A sensor that freezes during a long uninterrupted
        drive is only suspected after the next engine-off gap (documented
        limitation).
        """
        cfg = self.config
        gap = None if state.last_coolant_us is None else now - state.last_coolant_us
        if gap is not None and gap >= self._stuck_gap_us and not state.stuck:
            state.anchor, state.anchored, state.deviations = state.last_coolant, 0, 0
        state.last_coolant, state.last_coolant_us = value, now
        if state.anchor is None:
            return False
        if abs(value - state.anchor) <= cfg.stuck_tolerance_c:
            state.deviations = 0
            state.anchored += 1
            if state.anchored >= cfg.stuck_min_samples:
                state.stuck = True
            return True  # provisional in OBSERVING, established in SUSPECTED
        state.deviations += 1
        if state.stuck and state.deviations < 2:
            return True  # one deviation is not recovery evidence: the suspicion stands
        state.anchor, state.anchored, state.stuck, state.deviations = None, 0, False, 0
        return False
