# Architecture and safety boundaries (v0.1)

## Purpose

Demonstrate repeatable **anomaly detection**, not fault prediction. A deviation can arise from driving conditions, sensor behavior, maintenance, or real mechanical deterioration. No root cause is inferred.

## Minimal flow

1. Read two local CSV datasets: baseline (earlier) and observations (later).
2. Reject invalid timestamps, non-finite values, malformed rows, reversed per-vehicle chronology, and overlapping reference/observation time windows.
3. Group reference readings by vehicle, engine-RPM bucket (500 RPM), and speed bucket (20 km/h).
4. Require at least five reference points in the matching bucket; take its median coolant temperature.
5. Compare later readings to that frozen reference; a second consecutive same-direction deviation of at least 8 C produces anomaly. One produces suspected_anomaly.
6. Emit JSONL, preserving evidence (observation, reference, delta). If no matched baseline exists, emit insufficient_baseline.

Default bucket widths, reference count, thresholds, and persistence are **illustrative**, not proven safe for any make, model, or duty cycle. Coarse buckets do not account for ambient temperature, load, sensor calibration, or many operating regimes.

## Boundaries

- No writes to a vehicle ECU; no control of safety-critical actuators.
- No claims about failure probabilities, component lifetime, or whether a vehicle is safe.
- No network communication or storage of personal vehicle location/identity beyond the user-provided local CSV.
- No automatic model adaptation from the observations: prevents contaminating the reference with the anomaly under evaluation.
- Strict chronological split by vehicle helps avoid a common form of evaluation leakage; it is not sufficient alone to prove external validity.
- Dataset lineage, consent, compatibility, and relevant safety/regulatory reviews are mandatory before trials with real vehicles.

## Evolution criteria

Only introduce a new component after a measurable requirement exists. Before calling a model predictive, evaluate it on previously unseen vehicles and chronological holdouts with ground-truth failures, report sensitivity/precision, false alerts, lead time, uncertainty, and missing-data behavior. Keep hardware acquisition and physics-based / machine-learning models behind separate interfaces when they become necessary.

The aerospace pathway requires a separately validated implementation and is **not** certified or flight-ready through code reuse.
