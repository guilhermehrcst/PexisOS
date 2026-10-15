# Independent adversarial review of v0.2 (this PR)

After held-out run 1, two independent reviewers, with no part in writing the
code, attacked the changes in parallel. Both were read-only on the repository.
One did code and security review, the other scientific validity. Every finding
was first verified, and each fix was reproduced before it was made. This page
records each finding and its disposition. ADR 0001 §7 records the resulting
design changes, and [BENCHMARK.md](BENCHMARK.md) reports held-out run 2.

What held up in both reviews:

- **Default-configuration detection.** 1,200 random end-to-end runs never
  wrote to stdout on error and never raised a traceback, output stayed 1:1 with
  input rows, and every `normal` and `confirmed_anomaly` satisfied its stated
  invariant.
- **Legacy CLI.** Output was byte-identical to v0.1 on 742 fuzzed valid pairs.
- **Generator causality.** Reference rows matched the no-injection control.
- **Statistics.** The statistics code was exact to 50-digit precision.
- **Numbers.** More than 30 numbers in the documentation matched the results files.

## Code and security review: 16 findings

| ID | Severity | Finding | Disposition | Evidence |
|---|---|---|---|---|
| H1 | high | Duplicate-column check was O(n²) on an unbounded header. A 643 KB header took 84 s; the legacy reader was a regression introduced by the F-11 fix. | Fixed. Width is checked first and duplicates are counted with `Counter`. | `test_wide_header_is_rejected_in_linear_time` |
| M1 | medium | Records could span lines through quoted newlines. A 64 MiB record caused `MemoryError`, and one stray quote folded 1000 rows into one. | Fixed. A line with an odd quote count is rejected. | `test_records_may_not_span_lines` |
| M2 | medium | Bit-equality stuck rules missed a sensor dithering by 1 LSB, so F-01 was not fixed. | Fixed. Tolerance-based rule after an engine-off gap. | `test_dithering_frozen_sensor_is_suspected`; flicker scenario |
| M3 | medium | One glitched row (1970 or 2199) aborted the run for the whole fleet. | Fixed. Per-record `reference_overlap` against the last contributing reference sample. | `test_excluded_reference_glitch_cannot_deny_a_vehicle`, `test_reference_overlap_is_quarantined_per_record` |
| M4 | medium-low | Memory was 4× the file size for non-ASCII input, and reference contexts were unbounded. | Fixed. Byte snapshot with per-line decoding, plus `max_contexts`. | `test_non_ascii_identifier_*`, `test_context_limit` |
| M5 | medium-low | The flatline rule flagged healthy steady cruise and **silenced a real 118 C alarm**. | Fixed. Rule removed; the absolute barrier evaluates suspected readings. | `test_long_steady_cruise_is_never_suspected`, `test_stuck_high_sensor_still_alarms` |
| L1 | low-medium | Readings above 150 C counted as "implausible", disabling the absolute barrier. | Fixed. Range is now -40..215 with sentinels; unreachable limits are rejected. | `test_readings_above_150_c_are_alarms_not_implausible` |
| L2 | low-medium | The summary was written after the output, could overwrite an input, and was created with mode 0600. | Fixed. The destination is prepared and checked before output, and the umask is respected. | `test_summary_destination_is_checked_before_any_output` |
| L3 | low | `+05:99` was accepted as an offset. | Fixed. Offset hours and minutes are validated. | `test_timestamp_profile` |
| L4 | low | Some configuration values crashed the run after partial output, emitted `Infinity`, or accepted duplicate keys. | Fixed. Magnitudes are bounded, overflow-safe checks are used, duplicate keys are rejected, JSON is written with `allow_nan=False`, and recursion is guarded. | `test_invalid_configurations_are_rejected`, `test_json_file_hazards_are_value_errors` |
| L5 | low | The size flag could overflow or preallocate memory, and a full output device produced a traceback. | Fixed. The flag is bounded to 1-4096, files are read at their real size, and write failures exit 1. | `test_numeric_flags_are_bounded`, `test_output_device_full_exits_1_without_traceback` |
| L6 | low | "Byte-identical legacy output" was false above 64 MiB. | Fixed. The claim is qualified and the legacy CLI gets `--max-file-mb`. | `test_legacy_accepts_a_file_size_flag` |
| L7 | low | Raw duplicate column names reached stderr, and UTF-8 error line numbers were wrong. | Fixed. Names go through `repr()`, and decoding is per line. | `test_duplicate_names_are_escaped_in_diagnostics`, `test_invalid_utf8_line_numbers_*` |
| L8 | low | Conflicting-duplicate detection depends on arrival order. | **Documented, not changed.** Detecting late conflicts needs per-vehicle history (O(window) memory), and resetting runs on every late row would break the duplicate/late-row invariance tested in `MetamorphicTests`. | BENCHMARK.md limitations |
| L9 | low | Rounding to microseconds could break the "≥ 3 samples" guarantee. | Fixed. Validation and comparison both use integer microseconds. | config tests |
| L10 | low | `platform.platform()` spawned `uname -p`. | Fixed. Uses `os.uname()`, with a runtime process-creation spy. | `test_no_process_is_spawned_at_runtime` |

## Scientific review: 17 findings

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| F1 | critical | Hypothesis verdicts did not discriminate: a detector that never outputs anything passed H1-H5, and the amended H2 passed with recall 0. | Fixed. Coverage floor and per-trip false-alert conditions added; H2a/H2b require an excess-recall interval above 0. The original H2 is reported ("not supported"). `HypothesisCriteriaTests` checks that null and always-alarm detectors support nothing. |
| F2 | high | The placebo control saturated (unbounded, direction-agnostic windows), so "v0.1 shows no demonstrated detection" was an artifact. | Fixed. Direction-aware, 24 h horizon for abrupt faults. v0.1 does detect overheats beyond chance; documented. |
| F3 | high | Vehicle id determined the label in every seed, so a rule using ids could game the benchmark. | Fixed. Roles are drawn per seed (`test_fault_roles_are_drawn_per_seed`), and runners receive rows only. |
| F4 | high | The zero-false-alert result depends on generated warm-ups ending inside the 900 s window. | Measured. Slow warm-up sweep (×0.6, ×0.5); the dependency is stated next to H1. |
| F5 | high | H4 was circular (gaps always longer than `stuck_gap_s`, bit-identical freezes only) and the stuck rule's benefit was misattributed. | Fixed and measured. Tolerance rule, flicker scenario, no-stuck ablation; attribution corrected. |
| F6 | high | The abrupt-fault delay pooled overheat and thermostat events. | Fixed. Delays are reported per fault kind. |
| F7 | high | H3 was supported by abstention; "coverage while towing" was misreported. | Fixed. Coverage is measured on affected rows; the H3 verdict says "supported only by abstention" when that is the case. |
| F8 | medium | False-alert exposure included hours where a false alert was impossible; true-alert credit ignored direction. | Fixed. Eligible exposure, direction-aware attribution, misdirected alerts counted separately. |
| F9 | medium | Poisson and Wilson intervals ignored clustering by vehicle. | Fixed. Vehicle-cluster bootstrap. |
| F10 | medium | Pre-registration wording ("fixed before any run") was not supported by git. | Fixed. Exact timeline in ADR §4, §6, §7. |
| F11 | medium | The "dispersion-aware threshold" benefit came from the lower threshold, not the robust scale. | Measured. `v0.2-fixed-4C` ablation; relabelled. |
| F12 | medium | The episode definition inflated v0.1's alert count about 3×. | Fixed. Trips with a false alert are reported. |
| F13 | low | Document/result mismatches (84 alerts attributed to seeds 201-205, "identical in every scenario", runtimes). | Fixed in the rewritten BENCHMARK.md. |
| F14 | low | H5 holds by construction; H6 mixed hot and cold; H3 checked only one detector. | Fixed. Labels and criteria aligned. |
| F15 | low | Headline totals counted the same driving about 12 times (paired design). | Fixed. Unique exposure reported. |
| F16 | low | Recall 1.0 was guaranteed by the parameter ranges. | Measured. Subtle-overheat scenario (3-7 C). |
| F17 | low | `suspected_anomaly` is never scored; runners could reach ground truth. | Runners fixed. Unscored `suspected` documented. |

## Process notes

- Detector changes made after run 1 are listed in ADR 0001 §7. No detector
  parameter value was tuned. The test seeds were reused for run 2; that
  weakness is disclosed rather than hidden.
- A mutation check was re-run after the fixes: 18 deliberately injected bugs,
  including one for each safety fix above, were all caught by the suite.
