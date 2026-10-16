"""Synthetic vehicle: classic-CAN frames decoded through a test signal table.

Independent of the v0.4 CAN replay lab (PR #10): no shared code, no cantools,
no file input. Frames are generated in memory; nothing opens a CAN interface
or transmits anything.

``SIGNAL_TABLE`` is a test-DBC expressed as data, with DBC semantics: Intel
(little-endian) byte order, start bit, bit length, signedness, factor, offset,
unit, and a plausible range. Decoding is deliberately conservative:

* an arbitration id absent from the table yields an ``unknown`` observation
  with an EMPTY payload: unknown bytes are never presented as measurements;
* a frame whose length differs from the declared DLC yields ``unknown`` with
  ``length_mismatch`` and an empty payload;
* a decoded value outside the plausible range is kept but the observation is
  ``unknown`` (flag ``out_of_range``), so it is never used as a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import Capability, EntitySpec, SignalSpec
from ._common import T0, Noise, arrival_order, observation

ADAPTER_ID = "synthetic:vehicle-can-v0"
GENERATOR = "pexislink.fabric.adapters.vehicle"
ENTITY_ID = "demo:vehicle:synthetic-01"
INTERVAL_S = 10


@dataclass(frozen=True)
class CanSignal:
    name: str
    start_bit: int
    length: int
    signed: bool
    factor: float
    offset: float
    unit: str
    minimum: float
    maximum: float


@dataclass(frozen=True)
class CanMessage:
    name: str
    dlc: int
    signals: tuple[CanSignal, ...]


SIGNAL_TABLE: dict[tuple[int, bool], CanMessage] = {
    (0x101, False): CanMessage("SYNTH_ENGINE", 8, (
        CanSignal("coolant_temp_c", 0, 8, False, 1.0, -40.0, "degC", -40.0, 150.0),
        CanSignal("engine_speed_rpm", 8, 16, False, 0.25, 0.0, "rpm", 0.0, 8000.0))),
    (0x102, False): CanMessage("SYNTH_POWER", 4, (
        CanSignal("battery_v", 0, 16, False, 0.01, 0.0, "V", 0.0, 32.0),
        CanSignal("ambient_temp_c", 16, 8, True, 1.0, 0.0, "degC", -40.0, 60.0))),
}


def _validate_table() -> None:
    names = set()
    for (frame_id, extended), message in SIGNAL_TABLE.items():
        assert 0 <= frame_id < (1 << (29 if extended else 11)) and 0 <= message.dlc <= 8
        used = 0
        for s in message.signals:
            mask = ((1 << s.length) - 1) << s.start_bit
            assert s.length >= 1 and s.start_bit + s.length <= 8 * message.dlc, s.name
            assert not used & mask, s.name  # no overlapping bits
            assert s.name not in names, s.name
            used |= mask
            names.add(s.name)


_validate_table()


def entity(entity_id: str = ENTITY_ID) -> EntitySpec:
    signals = tuple(SignalSpec(s.name, "number", s.unit, s.minimum, s.maximum)
                    for message in SIGNAL_TABLE.values() for s in message.signals)
    return EntitySpec(
        entity_id=entity_id, kind="vehicle.can", display_name="Synthetic vehicle 01 (no real identifiers)",
        adapter=ADAPTER_ID, capabilities=frozenset(c.value for c in Capability), signals=signals,
        expected_interval_s=INTERVAL_S / 2, max_latency_s=5, stale_after_s=60)


def decode_signal(signal: CanSignal, data: bytes) -> float:
    raw = (int.from_bytes(data, "little") >> signal.start_bit) & ((1 << signal.length) - 1)
    if signal.signed and raw >> (signal.length - 1):
        raw -= 1 << signal.length
    return round(raw * signal.factor + signal.offset, 6)


def encode(message: CanMessage, values: dict[str, float]) -> bytes:
    word = 0
    for s in message.signals:
        raw = round((values[s.name] - s.offset) / s.factor)
        if not (-(1 << (s.length - 1)) if s.signed else 0) <= raw < (1 << (s.length - (1 if s.signed else 0))):
            raise ValueError("value not encodable")
        word |= (raw & ((1 << s.length) - 1)) << s.start_bit
    return word.to_bytes(message.dlc, "little")


def frames(seed: int = 2, cycles: int = 360, start: int = T0 + 3 * 3600 * 1_000_000) -> list[tuple]:
    """A synthetic bus capture: (timestamp_us, arbitration_id, is_extended, data)."""
    noise = Noise(seed)
    engine, power = SIGNAL_TABLE[(0x101, False)], SIGNAL_TABLE[(0x102, False)]
    rpm, out = 1500.0, []
    for i in range(cycles):
        t = start + i * INTERVAL_S * 1_000_000
        rpm = min(4000.0, max(700.0, rpm + noise.gauss(60.0)))
        heating = max(0, i - (cycles - 12)) * 2.5
        coolant = round(90 + noise.gauss(0.8) + heating)
        engine_data = encode(engine, {"coolant_temp_c": coolant, "engine_speed_rpm": round(rpm * 4) / 4})
        if i == 200:
            engine_data = engine_data[:5]  # truncated frame
        out.append((t, 0x101, False, engine_data))
        battery = 0xFFFF * 0.01 if i == 250 else round(13.8 + noise.gauss(0.05), 2)
        out.append((t + 5_000_000, 0x102, False,
                    encode(power, {"battery_v": battery, "ambient_temp_c": 12 + round(noise.gauss(0.5))})))
        if i % 25 == 7:
            out.append((t + 2_500_000, 0x3A5, False, bytes([0xDE, 0xAD, 0xBE, 0xEF])))  # not in the table
    return out


def decode_frames(captured: list[tuple], seed: int = 2, entity_id: str = ENTITY_ID) -> list[dict]:
    out = []
    for index, (t, frame_id, extended, data) in enumerate(captured):
        common = {"event_id": f"vehicle-01-{seed}-{index:06d}", "entity_id": entity_id, "adapter": ADAPTER_ID,
                  "observed_at": t, "received_at": t + 200_000, "generator": GENERATOR, "seed": seed}
        message = SIGNAL_TABLE.get((frame_id, extended))
        if message is None:
            out.append(observation(kind="vehicle.can_unknown_frame", payload={}, status="unknown",
                                   flags=("unknown_frame",), **common))
        elif len(data) != message.dlc:
            out.append(observation(kind="vehicle.can_signals", payload={}, status="unknown",
                                   flags=("length_mismatch",), **common))
        else:
            out.append(observation(kind="vehicle.can_signals",
                                   payload={s.name: decode_signal(s, data) for s in message.signals}, **common))
    return arrival_order(out)


def generate(seed: int = 2, cycles: int = 360, entity_id: str = ENTITY_ID) -> list[dict]:
    return decode_frames(frames(seed, cycles), seed, entity_id)
