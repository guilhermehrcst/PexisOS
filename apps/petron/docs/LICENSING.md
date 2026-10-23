# PETRON licensing and third-party notices

- License: **Apache-2.0**, the PexisOS repository license (owner decision, 2026-10-22).
- `apps/petron/LICENSE` is a byte-identical copy of the repository's `/LICENSE`, so a
  standalone `petron` wheel or sdist carries the license text. `tests/test_monorepo_boundary.py`
  fails if the two ever differ.
- Package metadata: `license = "Apache-2.0"`, `license-files = ["LICENSE"]`.
- No `NOTICE` file exists at the repository root, so none is propagated.

## What PETRON contains

All source, tests, fixtures and documentation under `apps/petron/` were written for this
project. No third-party source code is vendored or copied. The E001 fixture (`calc`) and all
E001 evidence are synthetic.

## Third-party components PETRON refers to but does not distribute

| Component | Version | License (from its published metadata) | How PETRON uses it |
|---|---|---|---|
| Python standard library | ≥ 3.11 | PSF-2.0 | runtime |
| `anthropic` (Anthropic Python SDK) | `>=2.1,<3` (2.1.0 inspected) | Apache-2.0 | optional extra `[anthropic]`, imported only when a real provider is explicitly authorized |
| `mcp` (Model Context Protocol SDK) | `==2.2.0` | MIT | optional extra `[pexisos]`; installed from the core's hash-locked `requirements/fabric-mcp.txt` (its transitive dependencies are governed by that lock) |
| `python:3.11-slim` container image | digest `sha256:cce12316…ff8` | Python (PSF-2.0) plus Debian packages under their own licenses | pulled by the operator; referenced by digest, never redistributed by PETRON |
| Docker Engine | operator-installed | Apache-2.0 | external tool invoked through its CLI |

Installing an optional extra or pulling the image is the operator's act; those components keep
their own licenses. PETRON's package declares no mandatory dependency.

## Open-core inventory

When PR #21's public-source inventory is on the same base as this component, `apps/petron/`
needs an inventory rule (proposed: `repository-license`, with `fixtures/` and
`docs/experiments/e001-evidence/` as `generated-synthetic`). Inventory coverage is structural;
it is not a legal opinion, and `legal_signoff` remains the owner's to record.
