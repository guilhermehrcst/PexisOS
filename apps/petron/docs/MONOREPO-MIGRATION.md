# PETRON → PexisOS monorepo migration (2026-10-21)

Decision: ADR-0002. Branch: `feat/petron-v01-monorepo`, created from `main` @ `db097c8`
(verified against GitHub on 2026-10-21). Local only; nothing pushed.

## History

The six commits of the former local repository were imported with their paths rewritten
under `apps/petron/` (`git filter-repo --to-subdirectory-filter` on a clone, then replayed
linearly on `main`). Author, author date and message are unchanged; the SHAs are new.
Every imported commit's `apps/petron` tree is byte-identical to the original commit's root tree.

| Original (local repo) | Imported | Subject |
|---|---|---|
| `d913d7c` | `a161018` | feat: PETRON V0.1 core (M0+M1) |
| `10f58ff` | `55ce6c4` | feat(sandbox): measured Docker isolation probe and fail-closed tests (M2) |
| `8668cdf` | `977de36` | feat(connector): read-only PexisOS Agent Fabric MCP client (M3) |
| `6fd2328` | `f7b78fc` | test: close two mutation-check gaps; docs: architecture, threat model, README |
| `aaccd1e` | `2bf0610` | chore: drop unused import |
| `21687b8` | `77316a4` | docs(e001): PETRON-E001 infrastructure run with evidence |

`git subtree add` was evaluated and not used. It keeps the original SHAs but imports them
with root-level paths (`pyproject.toml`, `src/`, `tests/` at the repository root in those
commits), plus an unrelated second root and a merge commit; `git log`/`blame` on
`apps/petron` would not follow that history without subtree-aware options. The rewrite
gives a linear history where every commit touches only `apps/petron/`. The mapping above
replaces SHA continuity. No nested Git repository is kept.

## Pre-import screening (all commits, all blobs)

- Paths: no SQLite/state databases, caches, `__pycache__`, virtual environments,
  `egg-info`, archives, keys or `.env` files in any commit (43 paths, 49 blobs).
- Content: the only token-shaped strings are short synthetic test vectors in the redaction
  tests (`sk-ant-api03-zzzz…`, a three-byte fake PEM block); no real credential. No e-mail
  addresses in content; no private data. Only public IP `1.1.1.1` (egress test).
- Commit metadata: author `Claude <noreply@anthropic.com>` and `Claude-Session` trailers, as
  already present on 26 commits of `main`.
- `docs/PREFLIGHT-2026-10-21.md` mentions local paths of the development container and the
  former "separate repository" arrangement; kept as a dated historical record.

## Independence (measured)

- `pexislink` wheel and sdist built from this branch contain **no** PETRON file
  (root `packages.find where=["src"]`; sdist = LICENSE, PKG-INFO, README, pyproject, setup.cfg,
  src, tests).
- `tests/test_monorepo_boundary.py`: PETRON never imports `pexislink`, the core never imports
  `petron`, neither package depends on the other; shown to fail when either import is planted.
- A fresh `pip install -e apps/petron` installs PETRON alone (`pexislink` not importable).
- Agent Fabric contracts, controls and tests are untouched: the branch changes only
  `apps/petron/**` and adds `.github/workflows/petron.yml`.

## Open-core obligations (PR #21) — not discharged by this migration

Measured on a throwaway merge of this branch with PR #21's local head `a1b66bd` (PR #21 itself
not modified):

| PR #21 gate | Result |
|---|---|
| `scripts/ip_inventory.py --check` | **fails: 45 `unclassified_tracked_path: apps/petron/…`** |
| `scripts/check_open_core_boundary.py --check` | clear |
| `check_public_artifacts.py` on the branch's `pexislink` wheel + sdist | clear |
| PR #21 tests (`test_open_core_ci`, `test_open_core_regressions`, `test_evidence_proposal`) | 58 pass |
| PR #21 bypass detector applied by hand to `petron.yml` | no bypass found; **the detector only reads `tests.yml`** |

Candidate inventory rules (verified to give `inventory_complete: 175 tracked paths` on the
throwaway merge; **not committed anywhere**, for the owner and PR #21 to decide):

```json
{"path": "apps/petron/", "match": "prefix", "classification": "repository-license", "ownership_review": "pending"},
{"path": "apps/petron/fixtures/", "match": "prefix", "classification": "generated-synthetic", "ownership_review": "pending"},
{"path": "apps/petron/docs/experiments/e001-evidence/", "match": "prefix", "classification": "generated-synthetic", "ownership_review": "pending"}
```

Structural coverage is not a legal conclusion (`legal_signoff` stays `not_recorded`).

Decisions required before publication:

1. **License of PETRON.** Pushing this branch to the public repository publishes PETRON and,
   absent another notice, places it under the repository's Apache-2.0. If PETRON is meant to
   be proprietary or part of a future enterprise offering, it must not be pushed here.
2. Inventory classification of `apps/petron/` (rules above or others), to be added through
   PR #21's inventory once one of the two branches is rebased on the other.
3. Whether PR #21's CI-bypass contract should also cover `petron.yml` (or all workflows).
4. Third-party references: optional `anthropic` (installed only on request) and `mcp==2.2.0`
   (same hash-locked file as the core); the sandbox image is referenced by digest, not vendored.
