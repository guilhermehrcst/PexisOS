"""Write a SYNTHETIC operational-readouts file shaped like the publisher's
description (vehicle_id, time_step, 105 anonymous columns) to measure the
preflight's time and memory. It contains no Scania data and must never be
presented as such.

    python scripts/componentx_synthetic_scale.py --rows 200000 --vehicles 4000 --out DIR
    pexislink dataset preflight --source component-x-v3 --input-dir DIR --out manifest.json
"""

import argparse
import random
from pathlib import Path

FEATURES = 105


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--vehicles", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    if not 1 <= args.vehicles <= args.rows:
        parser.error("need 1 <= --vehicles <= --rows")
    args.out.mkdir(parents=True, exist_ok=True)
    header = ["vehicle_id", "time_step"] + [f"synthetic_f{i:03d}" for i in range(FEATURES)]
    base, extra = divmod(args.rows, args.vehicles)  # exactly --rows rows in total
    with open(args.out / "train_operational_readouts.csv", "w", encoding="ascii", newline="\n") as handle:
        handle.write(",".join(header) + "\n")
        for vehicle in range(args.vehicles):
            time, counters = 0.0, [0.0] * FEATURES
            for _ in range(base + (vehicle < extra)):
                time += rng.choice((2.0, 4.0, 6.2, 9.4))
                counters = [c + rng.randrange(0, 50) for c in counters]
                cells = [("" if rng.random() < 0.01 else f"{c:.1f}") for c in counters]
                handle.write(f"{9_000_000 + vehicle},{time:.1f}," + ",".join(cells) + "\n")


if __name__ == "__main__":
    main()
