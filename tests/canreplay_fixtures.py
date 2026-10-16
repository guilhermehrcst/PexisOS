"""SYNTHETIC fixtures for the v0.4 CAN replay lab.

Everything here was written for this project: the DBC, the message and signal
names (``SYNTH_*``), the identifiers and every payload. None of it comes from a
vehicle, an OEM database or any third-party log, and none of it describes a real
signal. The expected values are worked out by hand (see the comments) and by a
small bit-level oracle that does not use cantools.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pexislink.canreplay import HEADER, DecodeFailure, MessageSpec, SignalSpec

# 291 = 0x123 (11-bit). 2147483939 = 0x80000000 | 0x123: the DBC encoding of a 29-bit id 0x123.
SYNTHETIC_DBC = '''VERSION ""

NS_ :

BS_:

BU_: SYNTH

BO_ 291 SYNTH_STD: 8 SYNTH
 SG_ std_u16 : 0|16@1+ (0.1,-40) [-40|6513.5] "degC" SYNTH
 SG_ std_s8 : 16|8@1- (1,0) [-128|127] "" SYNTH
 SG_ std_s12 : 24|12@1- (0.5,0) [-1024|1023.5] "V" SYNTH

BO_ 2147483939 SYNTH_EXT: 4 SYNTH
 SG_ ext_u8 : 0|8@1+ (2,0) [0|510] "rpm" SYNTH
 SG_ ext_s16 : 8|16@1- (1,10) [-32758|32777] "" SYNTH

BO_ 1792 SYNTH_EMPTY: 0 SYNTH
'''

# (name, start bit, length, signed, scale, offset, unit): little-endian (Intel) layout, as in the DBC.
LAYOUT: dict[tuple[int, bool], tuple[str, int, tuple[tuple[str, int, int, bool, float, float, str | None], ...]]] = {
    (0x123, False): ("SYNTH_STD", 8, (("std_u16", 0, 16, False, 0.1, -40.0, "degC"),
                                      ("std_s8", 16, 8, True, 1.0, 0.0, None),
                                      ("std_s12", 24, 12, True, 0.5, 0.0, "V"))),
    (0x123, True): ("SYNTH_EXT", 4, (("ext_u8", 0, 8, False, 2.0, 0.0, "rpm"),
                                     ("ext_s16", 8, 16, True, 1.0, 10.0, None))),
    (0x700, False): ("SYNTH_EMPTY", 0, ()),
}

# Hand-checked expectations (payload hex -> values), independent of any decoder:
#   STD 10 27 FE 34 0F 00 00 00: std_u16 = 0x2710 = 10000 -> 10000*0.1-40 = 960.0
#                                std_s8 = 0xFE = -2 ; std_s12 = 0xF34 (bits 24..35) = -204 -> -102.0
#   EXT 7B 00 80 00:             ext_u8 = 0x7B = 123 -> 246 ; ext_s16 = 0x8000 = -32768 -> -32758
EXPECTED = {
    ("0x123", False, "1027FE340F000000"): {"std_u16": 960.0, "std_s8": -2, "std_s12": -102.0},
    ("0x123", True, "7B008000"): {"ext_u8": 246, "ext_s16": -32758},
}


def oracle(key: tuple[int, bool], data: bytes) -> dict[str, float | int]:
    """Bit-level Intel decoding of LAYOUT, written without cantools."""
    _, _, signals = LAYOUT[key]
    raw_all = int.from_bytes(data, "little")
    values: dict[str, float | int] = {}
    for name, start, length, signed, scale, offset, _unit in signals:
        raw = (raw_all >> start) & ((1 << length) - 1)
        if signed and raw >> (length - 1):
            raw -= 1 << length
        # integral scale and offset keep an integer, as a DBC with "(2,0)" means integer arithmetic
        integral = float(scale).is_integer() and float(offset).is_integer()
        values[name] = raw * int(scale) + int(offset) if integral else raw * scale + offset
    return values


class StubDecoder:
    """A standard-library decoder over LAYOUT, for CI without cantools. It is not a test of cantools."""

    name = "stub"
    version = "0"

    def __init__(self) -> None:
        self.messages: Mapping[tuple[int, bool], MessageSpec] = {
            key: MessageSpec(name, key[0], key[1], length, tuple(SignalSpec(s[0], s[6]) for s in signals))
            for key, (name, length, signals) in LAYOUT.items()}

    def decode(self, message: MessageSpec, data: bytes) -> Mapping[str, object]:
        key = (message.frame_id, message.is_extended_id)
        if len(data) != LAYOUT[key][1]:
            raise DecodeFailure("length_mismatch")
        return oracle(key, data)


def stub_factory(text: str, limits: object) -> StubDecoder:
    return StubDecoder()


def frames_csv(rows: list[str], *, newline: str = "\n", final_newline: bool = True) -> bytes:
    text = newline.join([HEADER, *rows])
    return (text + newline if final_newline else text).encode()


CLEAN_ROWS = [
    "2026-01-01T00:00:00Z,0x123,0,1027FE340F000000",       # SYNTH_STD
    "2026-01-01T00:00:00.010Z,0x123,1,7B008000",           # SYNTH_EXT: same numeric id, 29-bit
    "2026-01-01T00:00:00.020+01:00,0x00000123,1,7B008000",  # same instant written with an offset: earlier
    "2026-01-01T00:00:00.030Z,0x456,0,00",                 # unknown message
    "2026-01-01T00:00:00.040Z,0x123,0,1027",               # wrong length for SYNTH_STD
    "2026-01-01T00:00:00.050Z,0x700,0,",                   # zero-length message
]


def write_inputs(directory: Path, rows: list[str] | None = None, *, dbc: str = SYNTHETIC_DBC,
                 frames: bytes | None = None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    dbc_path, frames_path = directory / "synthetic.dbc", directory / "synthetic_frames.csv"
    dbc_path.write_text(dbc, encoding="utf-8")
    frames_path.write_bytes(frames if frames is not None else frames_csv(CLEAN_ROWS if rows is None else rows))
    return dbc_path, frames_path
