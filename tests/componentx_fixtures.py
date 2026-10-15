"""SYNTHETIC micro-fixtures for the Component X preflight tests.

Nothing in this file is, or is derived from, Scania data. Vehicle identifiers
(9xxxxx), column names (``synthetic_*``), values and labels are invented to
exercise the parser. They follow only the publisher's *described* layout
(``vehicle_id``, ``time_step``, anonymous feature columns), which the real
files have not been checked against.
"""

from __future__ import annotations

from pathlib import Path

FEATURES = ("synthetic_counter_a", "synthetic_bin_b_0", "synthetic_bin_b_1")
OPERATIONAL_HEADER = ("vehicle_id", "time_step") + FEATURES
SPLIT_IDS = {"train": (900001, 900002, 900003), "validation": (910001, 910002), "test": (920001, 920002)}


def operational_rows(ids: tuple[int, ...], steps: int = 4) -> list[str]:
    rows = []
    for vid in ids:
        for step in range(steps):
            # cumulative-looking synthetic values; time steps irregular on purpose
            rows.append(f"{vid},{step * 3 + (vid % 2)}.5,{100 + step * 7},{step},{2 * step}")
    return rows


def csv_text(header: tuple[str, ...] | list[str], rows: list[str], *, newline: str = "\n",
             final_newline: bool = True) -> str:
    text = newline.join([",".join(header), *rows])
    return text + newline if final_newline else text


def clean_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for split, ids in SPLIT_IDS.items():
        files[f"{split}_operational_readouts.csv"] = csv_text(OPERATIONAL_HEADER, operational_rows(ids)).encode()
        files[f"{split}_specifications.csv"] = csv_text(
            ("vehicle_id", "synthetic_spec_0", "synthetic_spec_1"),
            [f"{vid},Cat{vid % 3},Cat{vid % 2}" for vid in ids]).encode()
    files["train_tte.csv"] = csv_text(("vehicle_id", "length_of_study_time_step", "in_study_repair"),
                                      [f"{vid},{20 + vid % 5},{vid % 2}" for vid in SPLIT_IDS["train"]]).encode()
    for split in ("validation", "test"):
        files[f"{split}_labels.csv"] = csv_text(("vehicle_id", "class_label"),
                                                [f"{vid},0" for vid in SPLIT_IDS[split]]).encode()
    return files


def write_dataset(directory: Path, *, omit: tuple[str, ...] = (), overrides: dict[str, bytes] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    files = clean_files()
    files.update(overrides or {})
    for name, data in files.items():
        if name not in omit:
            (directory / name).write_bytes(data)
    return directory
