# Adversarial audit of PexisLink v0.1 (PR #1, head `e1844c6`)

**Scope.** `src/pexislink/core.py`, `cli.py`, tests and CI of PR #1, evaluated as
a research platform whose outputs could one day inform safety-relevant
decisions. **Method.** Reproduce the baseline; form hypotheses about failure
modes; *measure* each one with a probe against the unmodified code; turn every
confirmed probe into an executable test; rank by consequence. Hypotheses that
the probes disproved are listed too.

## Baseline reproduced

PR #1's claims were reproduced exactly before any change: 11/11 unit tests pass
on Python 3.11.17, 3.12.3 and 3.13.16; the demo prints `normal`,
`suspected_anomaly`, `anomaly`. PR #1's CI run (`unit-tests`) was green.

## Severity scale

- **High**: silently produces a wrong, safety-relevant conclusion on routine data.
- **Medium**: wrong conclusions under specific conditions, or a visible
  robustness/availability failure.
- **Low**: open-schema or hygiene issue whose failure direction is safe.

## Findings, ranked

Code references are to `e1844c6`. "Test" names the executable evidence in
`tests/test_v01_audit.py` (pins the v0.1 behaviour) and the v0.2 test that
asserts the corrected behaviour.

| ID | Severity | Finding | v0.1 code | Probe → test | v0.2 status |
|---|---|---|---|---|---|
| F-01 | High | **A frozen sensor is reported `normal` forever** (120/120 readings over 20 days). Silent false reassurance is the most dangerous failure direction. | `core.py:157-161`: no sensor-validity logic before the `normal` branch | P6 → `test_F01_*`; v0.2 `StuckSensorTests` | Fixed: physics-based stuck-sensor suspicion (identical across an engine-off gap, or flat for 45 min observed time) yields `insufficient_evidence`. Held-out benchmark: false reassurance 0.81 (v0.1) → 0.058 (v0.2). |
| F-02 | High | **Every ordinary cold start and every legitimate load increase is a confirmed anomaly.** A 25 → 88 C warm-up ramp gives 6 `anomaly` rows; +9.5 C from towing at the same rpm/speed gives `anomaly`. In a fleet this is an alert flood, and alert fatigue hides real alarms. | `core.py:95-97`: context is rpm and speed only; `core.py:136-143`: reference mixes warm-up and warm samples | P7, P8 → `test_F02_*`; v0.2 `WarmupTests`, `LoadContextTests` | Fixed for warm-up (segment-relative suppression). Load: only identifiable with an `engine_load_pct` signal; with it v0.2 abstains instead of alarming, without it the false alerts remain (measured, not hidden). |
| F-03 | High | **"Sustained" is a sample count, blind to time.** Two deviations 28 days apart, or 100 ms apart, become a confirmed anomaly. The meaning of `persistence` depends on the sample rate, which is never validated. | `core.py:146-170`: streak ignores timestamps | P1, P4 → `test_F03_*`; v0.2 `PersistenceTests` | Fixed: confirmation is a continuous duration (`confirm_s`) with a maximum evidence gap (`run_gap_s`). |
| F-04 | Medium-High | **Sensor failures are reported as engine anomalies.** 215 C and -40 C sentinels confirm as `anomaly`; -1000 C and rpm = 1e300 are accepted. | `core.py:53-64`: only finiteness and non-negative rpm/speed | P5 → `test_F04_*`; v0.2 `SensorValidityTests` | Fixed: physical plausibility ranges; implausible values are `insufficient_evidence`, never anomalies. |
| F-05 | Medium | **Persistence resets cause false negatives.** Ten consecutive +14 C deviations never confirm when rpm jitters around a bucket edge (the streak is keyed by context); one sample in an unseen context resets the streak. | `core.py:163-168` (signature includes context); `core.py:151-155` | P2, P3 → `test_F05_*`; v0.2 `test_context_change_keeps_run`, `test_unassessable_sample_is_neutral` | Fixed: context changes keep the run; unassessable samples are neutral but cannot bridge more than `run_gap_s`. |
| F-06 | Medium | **The absolute 8 C threshold ignores dispersion**, so the false-alarm rate is uncalibrated: +7 C in a zero-spread context is `normal`, ±10 C noise in a wide context is `anomaly`. | `core.py:158`; `core.py:139-143` (median only) | P18 → `test_F06_*`; v0.2 `ScaleTests` | Fixed: threshold = 4 × robust scale (1.4826·MAD), floored at the 1 C sensor resolution so quantised data cannot produce a zero threshold. |
| F-07 | Medium | **A fault already present in the reference period is learned as normal.** Overheating at 112-113 C throughout the reference is `normal` afterwards. Inherent to any self-referenced detector. | `core.py:136-143`: no reference-quality checks | P17 → `test_F07_*`; v0.2 `AbsoluteBarrierTests` | Mitigated, not fixed: reference-free 115 C barrier with an independent premise. Benchmark: it did *not* rescue the fully contaminated cases (their faults stay below 115 C); see BENCHMARK.md. |
| F-08 | Medium | **Fail-closed at the wrong granularity.** One exact duplicate row (routine in telemetry) or one late row aborts the analysis for the whole fleet; one empty cell aborts the file. | `core.py:86-92`, `125-126`, `56-58` | P9, P10 → `test_F08_*`; v0.2 `IntegrityTests` | Fixed: per-record quarantine with explicit states; a conflicting duplicate also discards the vehicle's evidence run. Leakage overlap remains fatal. |
| F-09 | Medium | **Uncaught `csv.Error`** (field > 131072 chars): traceback and exit code 1 instead of the documented error exit 2. | `cli.py:26` catches only `ValueError, OSError` | P11 → `test_F09_*` | Fixed in `core.read_samples` (TDD, red first). |
| F-10 | Medium | **Unbounded memory.** The whole input is materialised (200k rows: 7.0 MB file, 50.3 MB peak), and a non-regular input without newlines (`/dev/zero`) is read until `MemoryError`. | `core.py:70-83` | P20, P24 → `test_F10_*` | Fixed: bounded reads (size, line length, rows, vehicles) shared by v0.1 and v0.2 readers. |
| F-11 | Low-Medium | **Duplicate header silently resolved**: with two `coolant_temp_c` columns the last one (150) is used. | `core.py:71-74` | P12 → `test_F11_*` | Fixed in `core.read_samples` (TDD). v0.2 rejects duplicate and unknown columns. |
| F-12 | Low | **Open schema.** `2_000`, Arabic-Indic and full-width digits parse as numbers; week dates and basic-format timestamps are accepted; vehicle ids accept ANSI escapes and NUL; NFC vs NFD spellings split one vehicle in two (fail-safe direction: `insufficient_baseline`). | `core.py:41-61` | P13, P14, P16, P19 → `test_F12_*` | v0.1 frozen; v0.2 closes the schema (RFC 3339 profile, ASCII decimals, restricted id alphabet, |offset| ≤ 14 h, years 1970-2199). |
| F-13 | Low | **Verification gaps.** Happy-path-heavy tests, no benchmark, no property/metamorphic tests, single Python version in CI, actions pinned by tag rather than SHA. | `tests/test_core.py`, `.github/workflows/tests.yml` | inspection | Addressed except SHA pinning (could not resolve action SHAs from this environment; recommended). |

## Hypotheses the audit tried and failed to break (preserved)

| ID | Property | Evidence |
|---|---|---|
| S-1 | Per-vehicle state isolation under interleaving | P21; `test_S1_*`; v0.2 metamorphic `test_vehicle_isolation` |
| S-2 | Mixed UTC offsets are ordered by instant | P15; `test_S2_*`; v0.2 `test_timezone_re_expression_is_invariant` |
| S-3 | Overlapping reference/observation periods are rejected | P22; `test_S3_*`; v0.2 keeps it fatal, checked twice (pre-pass and per record) |
| S-4 | Unknown vehicle / context is `insufficient_baseline`, not `normal` | PR #1 tests; v0.2 `test_unseen_vehicle_alarm_but_never_normal` |
| S-5 | Calendar-edge offsets (`0001-01-01T00:00+14:00`) do not crash | P23 |

## Why the v0.1 rule was frozen rather than fixed

`core.analyze` is the comparator in the benchmark. Changing it would make every
"v0.2 vs v0.1" number meaningless. Its decision-rule defects are therefore
*pinned* by characterisation tests (a failure there means the comparator moved),
and only defects that crash, misread or exhaust resources were fixed in its
reader. The v0.1 CLI invocation still works and now prints a deprecation notice.

## Residual risks after v0.2 (not fixed by this work)

- **Sensor drift is indistinguishable from a real temperature change** with a
  single coolant signal; both barriers share the sensor (common-cause failure).
- **Unobserved confounders** (ambient extremes, payload without a load signal)
  still produce false alerts; v0.2 can only abstain when the signal exists.
- **A single far-future timestamp glitch** makes all later rows of that vehicle
  `out_of_order` (fail-safe and visible in the summary, but it kills coverage).
- **Warm-up is time-based**: in severe cold it can exceed `warmup_s`
  (hypothesis H6, measured in the benchmark).
- **Thresholds and windows are research defaults**, chosen a priori on physical
  reasoning, not validated on real vehicles.

Probe outputs: the probe script and its raw output are reproduced by the tests
above; exact commands and results are in the PR description.
