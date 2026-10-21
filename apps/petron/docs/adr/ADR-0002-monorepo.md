# ADR-0002 — PETRON lives in the PexisOS monorepo

- Status: accepted by the owner (2026-10-21); supersedes the "separate local repository"
  arrangement recorded in `docs/PREFLIGHT-2026-10-21.md`. ADR-0001 otherwise stands.
- Date: 2026-10-21

## Decision

PETRON is a component of the `guilhermehrcst/PexisOS` repository at `apps/petron/`. One
repository, two components that are integrable and operationally independent:

| | PexisOS core (`src/pexislink`) | PETRON (`apps/petron`) |
|---|---|---|
| Package | `pexislink` (root `pyproject.toml`, `packages.find where=["src"]`) | `petron` (`apps/petron/pyproject.toml`) |
| Mandatory dependencies | none | none |
| Imports the other | never | never (enforced by `tests/test_monorepo_boundary.py`) |
| Link | serves the read-only Agent Fabric over MCP stdio | launches that server as a child process, read-only allowlist |
| CI | `.github/workflows/tests.yml` (unchanged) | `.github/workflows/petron.yml` |

## Rules

1. The root package never ships PETRON: it does not appear in the `pexislink` wheel or sdist,
   and PETRON is not an optional dependency of the root package.
2. PETRON does not change Agent Fabric contracts or controls; it consumes them as published.
3. PETRON keeps its own install (`pip install -e apps/petron`) and its own tests.
4. The open-core obligations (public-source inventory, IP register, licensing) apply to
   `apps/petron/` like any other tracked path. Adding the folder does not discharge them; see
   `docs/MONOREPO-MIGRATION.md`.

## Consequences

- Cross-component changes can be reviewed and tested in one pull request.
- PETRON becomes public when this branch is pushed; licensing must be decided first.
- PETRON's history was imported with paths rewritten under `apps/petron/` (original SHAs
  recorded in the migration document).
