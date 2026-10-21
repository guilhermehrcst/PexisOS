# PETRON mission report — `petron-e001`

> **SIMULATED PROVIDER: the plan came from a deterministic script. This run demonstrates PETRON's infrastructure (state machine, gates, sandbox, verification, evidence), not autonomous problem solving.**

- Objective: Synthetic fixture: calc.stats.median() returns the wrong value for even-length input. Make the existing tests pass by fixing calc/stats.py only. Tests are protected.
- Final state: **completed**
- Spec sha256: `742d923c740afd9299dbfb010760cd40962e0f1060808e0bcd60c5675e14a6bf`
- Plan sha256: `97df38f807abe869e7bf10f6cf795bf4f20907ca5236cbcd8a02beb3fd8df6d8`
- Provider: `{"model": null, "provider": "deterministic", "script": "e001_fix_median", "simulated": true}`
- Event chain: intact (26 mission events)

## State transitions

- created → planning
- planning → awaiting_approval
- awaiting_approval → running
- running → verifying
- verifying → completed

## Approvals

- `97df38f807abe869…` by **e001-harness (Claude Code agent under the owner's PETRON V0.1 task authorization; synthetic fixture; not an independent human review)** via `cli`

## Isolation

- Backend `docker`: verified
  - PASS daemon_seccomp
  - PASS egress_blocked
  - PASS empty_bounding_set
  - PASS image_present_pinned
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

## Steps

- [0] `read_file`: ok
- [1] `replace_in_file`: ok
- [2] `run_tests`: ok

## Verification (runtime-owned)

- Baseline tests before change: exit 1 (defect reproduced)
- PASS changes_within_writable_paths
- PASS event_chain_intact
- PASS protected_paths_unchanged
- PASS source_tree_unchanged
- PASS tests_pass_in_sandbox
- Changed paths: calc/stats.py

## Diff

```diff
--- a/calc/stats.py
+++ b/calc/stats.py
@@ -12,4 +12,6 @@
         raise ValueError("median() of empty sequence")
     ordered = sorted(values)
     middle = len(ordered) // 2
-    return ordered[middle]
+    if len(ordered) % 2:
+        return ordered[middle]
+    return (ordered[middle - 1] + ordered[middle]) / 2
```

