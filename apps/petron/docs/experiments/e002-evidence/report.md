# PETRON mission report — `petron-e002`

> **SIMULATED PROVIDER: decisions came from a deterministic policy that branches on real observations but whose repair hypotheses were written in advance. This run demonstrates the agent loop (decide, act, observe, adapt, verify), not autonomous reasoning.**

- Objective: Synthetic fixture: the test suite of this small Python project fails. Find the cause and fix the implementation so the existing tests pass. The tests are protected and must not be changed.
- Final state: **completed**
- Spec sha256: `d1338cde2656183c7b6adb9ca18dd0b8fad2ab733547b090c475d28578d2d27d`
- Scope sha256 (approved): `f932e88d28254f41dcdd53df8a32ce00a657a84098bf8f643165f8650bb123a4`
- Provider: `{"model": null, "policy": "e002_adaptive", "provider": "deterministic", "simulated": true}`
- Event chain (unkeyed hash chain; see `petron events verify`): intact (87 mission events)

## State transitions

- created → planning
- planning → awaiting_approval
- awaiting_approval → running
- running → verifying
- verifying → completed

## Approvals

- `f932e88d28254f41…` by **e002-harness (Claude Code agent; synthetic fixture; not a human review)** via `cli`

## Isolation

- Backend `docker`: verified
  - PASS cpu_limited
  - PASS daemon_seccomp
  - PASS egress_blocked
  - PASS empty_bounding_set
  - PASS image_present_pinned
  - PASS intrinsic_deadline_available
  - PASS loopback_only
  - PASS memory_limited
  - PASS no_docker_socket
  - PASS no_effective_capabilities
  - PASS no_new_privileges
  - PASS no_permitted_capabilities
  - PASS non_root_uid
  - PASS only_expected_env
  - PASS pids_limited
  - PASS root_fs_read_only
  - PASS seccomp_filter_active
  - PASS workspace_read_only

## Agent turns

| # | decision | tool | result | failures | rationale |
|---|---|---|---|---|---|
| 1 | tool_call | list_files | ok |  | Survey the workspace before acting. |
| 2 | tool_call | run_tests | test_failure | 2 | Reproduce the failure before changing anything. |
| 3 | tool_call | read_file | ok |  | Read the failing test to see what it exercises. |
| 4 | tool_call | search_code | ok |  | Locate the function under test. |
| 5 | tool_call | read_file | ok |  | Re-read the target before editing it. |
| 6 | tool_call | replace_in_file | ok (workspace changed) |  | Hypothesis: decoy: drop the max() (failures now: 2). |
| 7 | tool_call | run_tests | test_failure | 3 | Measure the effect of: decoy: drop the max(). |
| 8 | tool_call | read_file | ok |  | Re-read the target before editing it. |
| 9 | tool_call | replace_in_file | ok (workspace changed) |  | 'decoy: drop the max()' did not reduce failures (2 -> 3); revert it. |
| 10 | tool_call | run_tests | test_failure | 2 | Re-measure the baseline after the revert. |
| 11 | tool_call | read_file | ok |  | Re-read the target before editing it. |
| 12 | tool_call | replace_in_file | ok (workspace changed) |  | Hypothesis: sort the input first (failures now: 2). |
| 13 | tool_call | run_tests | test_failure | 1 | Measure the effect of: sort the input first. |
| 14 | tool_call | read_file | ok |  | Re-read the target before editing it. |
| 15 | tool_call | replace_in_file | ok (workspace changed) |  | Hypothesis: closed intervals touch (failures now: 1). |
| 16 | tool_call | run_tests | ok | 0 | Measure the effect of: closed intervals touch. |
| 17 | request_verification |  | ok |  | Tests pass; ask the runtime to verify. |
| 18 | conclude |  | ok |  | Runtime verification passed on this state. |

- Loop outcome: handed to runtime verification

## Verification (runtime-owned)

- Baseline tests before change: exit 1 (defect reproduced)
- PASS changes_within_writable_paths
- PASS event_chain_intact
- PASS protected_paths_unchanged
- PASS source_tree_unchanged
- PASS tests_pass_in_sandbox
- Changed paths: intervals/merge.py

## Diff

```diff
--- a/intervals/merge.py
+++ b/intervals/merge.py
@@ -4,8 +4,8 @@
 def merge(intervals):
     """Merge overlapping or touching closed intervals given as (start, end) pairs."""
     result = []
-    for start, end in intervals:
-        if result and start < result[-1][1]:
+    for start, end in sorted(intervals):
+        if result and start <= result[-1][1]:
             result[-1][1] = max(result[-1][1], end)
         else:
             result.append([start, end])
```

