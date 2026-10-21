# PETRON mission report — `petron-e001-noiso`

> **SIMULATED PROVIDER: the plan came from a deterministic script. This run demonstrates PETRON's infrastructure (state machine, gates, sandbox, verification, evidence), not autonomous problem solving.**

- Objective: Synthetic fixture: calc.stats.median() returns the wrong value for even-length input. Make the existing tests pass by fixing calc/stats.py only. Tests are protected.
- Final state: **failed** (`step_failed:2:isolation_unavailable:no_isolation_backend`)
- Spec sha256: `1682836538efedd5b9ad2d71a78980c955832565e01774049dca4f1f235de4bf`
- Plan sha256: `97df38f807abe869e7bf10f6cf795bf4f20907ca5236cbcd8a02beb3fd8df6d8`
- Provider: `{"model": null, "provider": "deterministic", "script": "e001_fix_median", "simulated": true}`
- Event chain: intact (21 mission events)

## State transitions

- created → planning
- planning → awaiting_approval
- awaiting_approval → running
- running → failed — `step_failed:2:isolation_unavailable:no_isolation_backend`

## Approvals

- `97df38f807abe869…` by **e001-harness (Claude Code agent; synthetic fixture)** via `e001-harness`

## Isolation

- Backend `none`: NOT verified (`no_isolation_backend`)

## Steps

- [0] `read_file`: ok
- [1] `replace_in_file`: ok
- [2] `run_tests`: FAILED isolation_unavailable:no_isolation_backend

## Verification (runtime-owned)

- not reached

