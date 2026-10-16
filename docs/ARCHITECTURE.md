# Architecture and safety boundaries (v0.2)

## Purpose

Repeatable, explainable **anomaly detection** on vehicle telemetry, with explicit
evidence states. Not fault diagnosis, not failure prediction. A deviation can
come from driving conditions, sensor behaviour, maintenance or real mechanical
deterioration; no root cause is inferred.

## Data flow

```
CSV ──► ingest ──► quality ──► reference  (reference period: frozen statistics)
          │           │
          │           └──────► detect ──► JSON Lines + run summary
          │                     ▲
          └─ InputError ──► exit 2, no output (fail closed)

synth ──► rows ─────────────────► any detector ──► statuses ─┐
  └────► ground truth (separate) ──────────────────────────► evaluate ──► report
```

| Module | Responsibility | Must not |
|---|---|---|
| `ingest` | Bounded read, closed CSV schema, strict row syntax, row-level issue codes | interpret physics or time |
| `quality` | Per-vehicle replay in arrival order: integrity states, segments, warm-up, plausibility, OBD sentinels, stuck sensor | reorder or drop records silently |
| `reference` | Frozen per-vehicle, per-context median and robust scale from clean, warm samples; records each vehicle's last contributing sample (the leakage boundary) | learn from observations |
| `detect` | Four statuses, two barriers, duration-based confirmation | assert `normal` without a reference |
| `config` | Single validated source of every parameter (closed JSON schema) | accept unknown keys |
| `synth`, `evaluate`, `baselines`, `benchmark` | Offline research harness | be imported by detection code (enforced by `tests/test_architecture.py`) |
| `core` | Frozen v0.1 rule and API (comparator, backward compatibility) | change its decision rule |
| `canreplay`, `lab_cli` | v0.4 Lab A: offline file-to-file CAN/DBC replay; optional cantools imported inside the adapter; runtime guard against device, network and process access ([CAN-REPLAY-LAB-A.md](CAN-REPLAY-LAB-A.md)) | import or be imported by detection, preflight or evaluation code; import python-can; create a bus; transmit |
| `componentx` | v0.3 Stage A: read-only, streaming preflight and provenance manifest for locally supplied Scania Component X v3 files ([COMPONENT-X-PREFLIGHT.md](COMPONENT-X-PREFLIGHT.md)) | import or be imported by detection or evaluation code; decode outcome values; interpret anonymous columns |

## Invariants (each enforced by tests)

1. Exactly one assessment per input row, in input order; nothing dropped silently.
2. `normal` implies: a reference exists for this vehicle and context, the
   reading is valid, plausible, not a sentinel, not suspected stuck, outside
   warm-up, within the threshold, and below the absolute limit.
   Suspected-stuck readings are still evaluated by the absolute barrier.
   A frozen-sensor suspicion is a per-vehicle state machine (HEALTHY,
   OBSERVING, SUSPECTED; see `QualityTracker._stuck`): every reading whose
   integrity is unresolved is suspect, one outlier never clears an established
   suspicion, recovery needs two consecutive plausible deviating readings, and
   an established suspicion carries from the reference file into the
   observations (checked against an explicit model on random streams).
3. `confirmed_anomaly` implies at least `confirm_s` of continuous same-direction
   evidence with no gap above `run_gap_s` (checked against an independent
   brute-force oracle on random sequences).
4. Per-vehicle state isolation; results are invariant to other vehicles' rows,
   to UTC-offset re-expression, and to inserted exact duplicates or late rows.
5. An observation is assessed only if it is strictly after the last reference
   sample its vehicle's statistics were built from; otherwise it is reported as
   `insufficient_evidence` / `reference_overlap` (per record, never fleet-wide).
6. An invalid input file produces no assessments: structure, encoding and limits
   are validated for the whole file, and the summary destination is prepared,
   before the first line is written.
7. Detection code cannot import ground truth or the evaluation harness.
8. Identical inputs produce byte-identical outputs; the quick benchmark is
   byte-identical on Python 3.11, 3.12 and 3.13 (checked in CI against
   committed results).

## Boundaries

- No writes to a vehicle ECU; no control of actuators; no network access and
  no child processes (no network or subprocess imports, and a runtime test
  fails on any process creation during `detect` or `benchmark`).
- No claims about failure probability, component lifetime or vehicle safety.
- No storage of identity or location beyond the user-provided local CSV; the run
  summary records file names and SHA-256, not directory paths.
- The reference never adapts to observations, so an anomaly under evaluation
  cannot contaminate its own reference. The flip side (a fault present during
  the reference period is learned as normal) is a measured limitation.
- Dataset lineage, consent, licensing and safety/regulatory review are
  mandatory before any trial with real vehicles.

## Evolution criteria

Add a component only for a measured requirement. Before calling anything
predictive, evaluate it on real data with previously unseen vehicles,
chronological holdouts and labelled failure onsets; report sensitivity,
precision, false alerts, lead time, uncertainty and missing-data behaviour,
with a placebo control. Keep hardware acquisition, physics models and learned
models behind separate interfaces when they become necessary. The aerospace
pathway requires a separately validated implementation; code reuse confers no
certification.
