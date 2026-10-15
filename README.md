# PexisLink

**Open-source research in vehicle telemetry, anomaly detection, and predictive health.**

PexisLink compares a vehicle's later coolant-temperature readings with that same
vehicle's earlier readings under similar operating conditions, and states how
much evidence the data supports: `normal`, `suspected_anomaly`,
`confirmed_anomaly`, or `insufficient_evidence`. It is read-only, offline,
CPU-only and has no runtime dependencies.

> [!IMPORTANT]
> **This is research software, not a diagnostic or safety system.** An anomaly is a deviation from the vehicle's own reference. It is not evidence of a mechanical defect, not a failure probability and not a remaining-useful-life estimate. PexisLink does not predict failures. Its benchmark uses synthetic data written by the same project, so the numbers verify behaviour against documented assumptions; they are not real-world accuracy. Never use the output to decide whether a vehicle is safe to operate, and never use it to control vehicle systems.

## Quick start

Requires Python 3.11+.

~~~bash
python -m pip install -e .

# 1. Generate a deterministic synthetic scenario (telemetry + separate ground truth)
pexislink synth --scenario abrupt_anomaly --seed 1 --out demo

# 2. Assess it (JSON Lines, one assessment per observation row) and write a run summary
pexislink detect --reference demo/reference.csv --observations demo/observations.csv \
    --summary demo/summary.json > demo/assessments.jsonl

# 3. Reproduce the benchmark (quick: ~40 s; full held-out split: ~6 min on one core)
pexislink benchmark --profile quick --out reports/benchmark-quick

# Tests: standard library only
PYTHONPATH=src python -m unittest discover -s tests -v
~~~

## What an assessment means

| Status | Meaning |
|---|---|
| `normal` | Assessable against this vehicle's reference in this operating context, within threshold, below the absolute limit. "Consistent with its own past", **not** "healthy". |
| `suspected_anomaly` | A deviation that has not yet lasted `confirm_s` (300 s). |
| `confirmed_anomaly` | A same-direction deviation sustained for at least 300 s with no evidence gap above 90 s. Not a diagnosis. |
| `insufficient_evidence` | The data cannot support a claim. `reasons[0]` says why: e.g. `warmup_transient`, `no_reference_vehicle`, `no_reference_context`, `coolant_stuck_suspected`, `coolant_temp_c_implausible`, `duplicate`, `out_of_order`, `unplaceable`. |

Two independent barriers raise anomalies: a **relative** one (|reading − the
vehicle's reference median| ≥ 4 × robust spread, per operating context, after
warm-up) and a reference-free **absolute** one (≥ 115 C). Neither can veto the
other, and neither can assert `normal` without a matching reference. Every line
carries the evidence used: reference median, delta, threshold, reference sample
count, and how long the deviation has lasted.

## Input contract (closed schema)

Columns: `timestamp, vehicle_id, engine_rpm, speed_kmh, coolant_temp_c`, plus
optional `engine_load_pct` (used as context when present in both files).
Unknown or duplicate columns are rejected.

- Timestamps: RFC 3339 with explicit offset (`2026-10-02T08:00:00Z`,
  `...+05:30`), years 1970-2199.
- Numbers: plain ASCII decimals; `nan`, `inf`, `1_000` and non-ASCII digits are rejected.
- `vehicle_id`: 1-64 characters of `A-Z a-z 0-9 . _ : -`. Use pseudonymous ids;
  never commit real VINs, location traces or private telemetry.
- Row-level problems (an empty cell, a bad number) become reasons on that row's
  assessment; file-level problems (structure, size, chronology) stop the run
  with exit code 2 **before any assessment is written**.
- Limits: 64 MiB per file (`--max-file-mb`), 1024-character lines, 2 million
  rows, 10,000 vehicles (`--max-vehicles`).
- Reference timestamps must precede each vehicle's observations (leakage guard).

## Backward compatibility

- `pexislink --baseline B.csv --observations O.csv` still runs the v0.1 rule
  with byte-identical output, and prints a deprecation notice on stderr.
- `pexislink.analyze`, `Sample`, `read_samples` and `Assessment` keep their
  v0.1 behaviour. They are frozen as the benchmark comparator and have known
  defects ([audit](docs/AUDIT-v0.1.md)). `read_samples` now bounds its input and
  reports CSV structure errors and duplicate headers as `ValueError`.
- v0.2 output uses `schema: pexislink.assessment.v2`. Status mapping from v0.1:
  `anomaly` → `confirmed_anomaly`, `insufficient_baseline` → `insufficient_evidence`.
- On the tiny v0.1 example files, v0.2 returns `insufficient_evidence`
  (`warmup_transient`): three readings within two minutes of the first
  observation cannot establish anything under v0.2's evidence rules.

## Evidence

- [Adversarial audit of v0.1](docs/AUDIT-v0.1.md): 13 ranked findings, each
  reproduced by a test in `tests/test_v01_audit.py`.
- [ADR 0001](docs/adr/0001-v0.2-offline-detector-architecture.md): architecture,
  rejected alternatives, pre-registered parameters and hypotheses.
- [Benchmark](docs/BENCHMARK.md): method, held-out results, scientific
  validity, limitations, resource use. Raw results: `reports/`.
- [Architecture and safety boundaries](docs/ARCHITECTURE.md).

## Open source

Apache-2.0. No runtime dependencies; tests use only the standard library. No
real telemetry or third-party data is included: all benchmark data is
generated deterministically by `pexislink synth`.
