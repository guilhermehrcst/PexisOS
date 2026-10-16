# PexisOS D1 IP and Public Boundary Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Audit the public PexisOS source/licensing baseline and introduce testable protections against accidental private-code distribution, while drafting (not implementing) a safe offline evidence exchange contract.

**Architecture:** Keep the Apache-2.0 PexisOS Core and its `pexislink` Python package entirely unchanged. Add repository-only standard-library inventory and static/package checks, CI verification, and a synthetic-only public evidence-envelope proposal; all commercial algorithms and Cloud execution remain unimplemented and private.

**Tech Stack:** Python 3.11+ standard library (`ast`, `tomllib`, `pathlib`, `unittest`, `zipfile`, `tarfile`, `hashlib`, `json`), Git CLI for tracked-path inventory, GitHub Actions; existing setuptools packaging. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-10-16-pexisos-open-core-proprietary-boundary-design.md`, accepted in owner conversation, proposed publicly in PR #19.

## Global Constraints

- Scope is **D1 readiness only**: source/IP inventory, open/private boundary guard, public artifact inspection, and a proposal plus **synthetic fixture** for later D2 review. No executable exporter, Cloud API, private enterprise implementation, repo creation, license enforcement, runtime policy change, or merge to `main`.
- Already published PexisOS Core stays Apache-2.0 and useful offline; license notices and contributor rights are not retroactively revoked. Do not infer legal ownership or patent rights from a green CI result.
- Preserve Python 3.11–3.13, `pexislink` imports/CLI/schema namespaces, optional MCP stdio and Lab A file-to-file read-only operation.
- Existing Agent Fabric decisions B1–B6 remain enforced. In particular, **no HTTP/SSE/remote MCP**, real hardware, writable capability, collection of private telemetry, authentication shortcuts or new stable schema.
- The current source `src/pexislink` must not import or load `pexisos_enterprise` or `pexisos_cloud`, nor package code or secrets from `private/`, `enterprise/` or `cloud/`.
- Analysis scripts are **repo/CI tooling only** under `scripts/`. Core code paths never invoke them or contact the network. Packaging checks are evidence, not a substitute for auditing all source history, secrets, dependencies, and releases.
- The public evidence proposal uses the already existing `pexislink.observation.v0` vocabulary and the synthetic provenance values `synthetic_fixture` / `project_original`. It is not an authorization token or an implemented API.
- Keep ongoing PR #13 (multi-provider lab) independent. Do not overwrite its branch or its changes; reconcile CI after a later approved merge.
- A security-relevant execution PR requires a review of its exact final HEAD and full CI; neither green CI nor an unsigned content hash alone proves trustworthy origin.

## Review Focus

The following are the five highest-risk input classes or mistakes to test explicitly in the owning tasks:

1. **New tracked file outside the inventory's declared roots:** report the path, never silently categorize it as project-owned. Covered by Task 1 `test_unclassified_tracked_file_is_a_failure`.
2. **Private import hidden in a valid package file:** static imports, from-imports, and a literal-string `importlib.import_module` must trigger a public-side violation. Covered by Task 2 `test_rejects_static_and_literal_dynamic_enterprise_imports`.
3. **Private file concealed inside an otherwise valid wheel/tar archive:** refuse path traversal and embedded `.env` or private package names; never echo the file body in the error. Covered by Task 3 `test_rejects_private_archive_paths_and_path_traversal`.
4. **Synthetic bundle with forged authority/authenticity claims or tampered content:** the proposal must never turn operator-declared text or SHA-256 into verified authorization or identity. Covered by Task 4 `test_proposed_example_is_unattested_and_digest_matches` and `test_tampered_copy_does_not_match_digest`.
5. **A working development branch with the new checks accidentally omitted from CI:** a missing guard job must fail the workflow-contract regression before enabling the job. Covered by Task 5 `test_workflow_includes_d1_boundary_gates`.

---

## File Ownership Map

| Path | One responsibility |
| --- | --- |
| `scripts/ip_inventory.py` | Deterministic classification/completeness audit of tracked repository paths; not legal ownership certification |
| `docs/ip/public-source-inventory.json` | Reviewable source-category rules for the current public repository tree |
| `docs/ip/OPEN-CORE-IP-REGISTER.md` | Contributor/licensing questions, review evidence, outstanding sign-offs and third-party notices |
| `tests/test_ip_inventory.py` | Inventory stability and negative categorization tests |
| `scripts/check_open_core_boundary.py` | Exact forbidden import roots, package references and private tracked-file path checks |
| `tests/test_open_core_boundary.py` | Boundary mutation-style failures and legitimate-Core controls |
| `scripts/check_public_artifacts.py` | Inspect built wheel/tar archives for unexpected private paths without extracting them |
| `tests/test_public_artifacts.py` | Synthetic malicious and clean archives; no real credentials |
| `docs/ip/OFFLINE-EVIDENCE-EXCHANGE-PROPOSAL.md` | Draft operator-authorized, offline-only handoff semantics for future D2 |
| `examples/evidence/synthetic-bundle-v0.json` | One minimal, deterministic, explicitly **unattested** synthetic contract example |
| `tests/test_evidence_proposal.py` | Fixture/document consistency, digest and absence of claims of verified identity |
| `tests/test_open_core_ci.py` | CI contract guard for newly required repository-level checks |
| `.github/workflows/tests.yml` | Add one `open-core-boundary` CI job; retain existing jobs |
| `README.md` | Link to accepted architecture, D1 audit and draft export contract; no changed quick-start instructions |

No `src/pexislink` runtime file, `pyproject.toml`, `LICENSE` or existing benchmark snapshot is modified in this D1 plan.

### Task 1: Build the public source/license inventory

**Files:**
- Create: `scripts/ip_inventory.py`
- Create: `docs/ip/public-source-inventory.json`
- Create: `docs/ip/OPEN-CORE-IP-REGISTER.md`
- Test: `tests/test_ip_inventory.py`

**Interfaces:**
- Produce: `tracked_paths(repo_root: Path) -> tuple[str, ...]` using `git ls-files -z`, sorted and normalized repository-relative paths.
- Produce: `classify_path(path: str, rules: Sequence[dict[str, str]]) -> str | None`; exact root-file matches before prefix-directory matches, with no "default proprietary" or guessed copyright ownership.
- Produce: `validate_inventory(paths: Sequence[str], manifest: dict) -> list[str]`; deterministic sorted error codes/paths when the manifest is malformed, a tracked path is uncovered, or a rule has an unknown classification.
- CLI: `python scripts/ip_inventory.py --check` exits 0 only when the inventory is **complete and structurally valid**; this does NOT mean human license review was performed.

- [ ] **Step 1: Add failing unit tests.** `test_unclassified_tracked_file_is_a_failure` supplies `("LICENSE", "src/pexislink/core.py", "unexpected/algorithm.py")` and requires a finding naming `unexpected/algorithm.py`; `test_repository_file_order_does_not_change_results` permutes the same tracked paths and checks stable output; `test_review_status_is_not_inferred_from_license_field` ensures `legal_signoff: "not_recorded"` remains unverified.
- [ ] **Step 2: Verify RED.** Run `PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_ip_inventory.py' -v`. Expected: failure because the inventory functions do not exist; no silent skips.
- [ ] **Step 3: Implement minimum inventory.** Manifest schema `pexisos.public-source-inventory.v0`, rule entries with keys `path`, `match` (`exact` or `prefix`), `classification` (`repository-license`, `generated-synthetic`, `third-party-reference`, `review-required`), and `ownership_review` (`pending` or `reviewed`). Cover current top-level paths: `.github/`, `.gitignore`, `LICENSE`, `README.md`, `docs/`, `examples/`, `pyproject.toml`, `reports/`, `requirements/`, `scripts/`, `src/` and `tests/`. Set review status `pending` by default and explicitly distinguish third-party requirement references from vendored code. Do not record an individual as copyright owner without verified evidence.
- [ ] **Step 4: Verify GREEN.** Run targeted test above, then `python scripts/ip_inventory.py --check` on the real repository. Expected: complete inventory with zero missing paths; separate manual sign-off remains `not_recorded`.
- [ ] **Step 5: Document manual follow-up.** In `OPEN-CORE-IP-REGISTER.md` record the precise questions (prior license grants, contributors and CLA/permission evidence, dependency notice obligations, third-party code vs references, future proprietary ownership) and an **explicit unresolved legal-review gate**. No private commercial source, people’s personal details or trade secrets in the public file.
- [ ] **Step 6: Commit.** `git add scripts/ip_inventory.py docs/ip/public-source-inventory.json docs/ip/OPEN-CORE-IP-REGISTER.md tests/test_ip_inventory.py && git commit -m "chore: inventory public PexisOS source and IP review gates"`.

### Task 2: Enforce a one-way public/private source boundary

**Files:**
- Create: `scripts/check_open_core_boundary.py`
- Test: `tests/test_open_core_boundary.py`

**Interfaces:**
- Consume: current `src/pexislink/` and public `pyproject.toml`, plus tracked-file paths from Task 1.
- Produce: `inspect_imports(source: str, filename: str) -> list[str]`, `inspect_tracked_paths(paths: Sequence[str]) -> list[str]`, and `inspect_packaging(pyproject: dict) -> list[str]`.
- CLI: `python scripts/check_open_core_boundary.py --check` exits nonzero if any forbidden private package or tracked path appears. Findings must be bounded and contain names/locations, never source lines or secret values.

- [ ] **Step 1: Write RED tests.** `test_rejects_static_and_literal_dynamic_enterprise_imports` exercises `import pexisos_enterprise`, `from pexisos_cloud import client` and `importlib.import_module("pexisos_enterprise.rules")` in synthetic source; `test_rejects_private_tracked_paths` supplies `private/engine.py` and `src/pexisos_enterprise/__init__.py`; `test_normal_open_core_import_is_allowed` supplies `from pexislink.fabric import Fabric`.
- [ ] **Step 2: Verify RED.** Run `PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_open_core_boundary.py' -v`. Expected: missing guard implementation.
- [ ] **Step 3: Implement checks.** Parse imports with `ast`; check exact private roots `pexisos_enterprise` and `pexisos_cloud` for static and literal-string dynamic imports. Flag tracked `private/`, `enterprise/`, `cloud/`, `src/pexisos_enterprise/`, `src/pexisos_cloud/`, `.env` and any `*.pem`/`*.key` file. Parse `pyproject.toml` using `tomllib` and reject private runtime/build/package references while preserving the existing `pexislink` configuration. Unknown computed imports cannot be proved absent by AST; document that as a review limitation.
- [ ] **Step 4: Verify GREEN.** Run targeted test above, `python scripts/check_open_core_boundary.py --check`, and `PYTHONPATH=src python -m unittest discover -s tests -p 'test_architecture.py' -v`. Expected: all PASS and no private runtime dependency.
- [ ] **Step 5: Commit.** `git add scripts/check_open_core_boundary.py tests/test_open_core_boundary.py && git commit -m "test: enforce open core source import boundary"`.

### Task 3: Refuse private paths in public build artifacts

**Files:**
- Create: `scripts/check_public_artifacts.py`
- Test: `tests/test_public_artifacts.py`

**Interfaces:**
- Produce: `inspect_archive(archive_path: Path) -> list[str]` supporting wheel/zip and source/tar packaging and returning stable, redacted path-only findings.
- CLI: `python scripts/check_public_artifacts.py --wheel PATH` validates a present wheel, nonempty payload, no malformed/traversal entries and no private file paths. Optional `--sdist PATH` applies the same rules if an sdist is actually built. The CI gate must not claim an unbuilt artifact was inspected.

- [ ] **Step 1: Write RED tests.** Use `zipfile` and `tarfile` to create disposable synthetic archives. `test_rejects_private_archive_paths_and_path_traversal` covers `pexisos_enterprise/engine.py`, `private/analysis.py`, `../outside.txt` and `.env`; `test_accepts_clean_pexislink_wheel` contains only `pexislink/__init__.py` and normal metadata; `test_error_never_prints_archive_content` ensures a fake secret marker inside rejected content does not appear in returned messages.
- [ ] **Step 2: Verify RED.** Run `PYTHONPATH=scripts python -m unittest discover -s tests -p 'test_public_artifacts.py' -v`. Expected: missing scanner implementation.
- [ ] **Step 3: Implement scanner.** Read archive member names without extraction, normalize POSIX separators and strip only the single normal sdist top-level directory. Reject absolute paths, `..`, encrypted/malformed ZIP archives, symlink-like tar members, unreadable or empty archives, and a documented exact list of forbidden private roots/sensitive filenames. Do not broadly scan arbitrary text and claim "no secrets": patterns are a tripwire, not a source confidentiality proof.
- [ ] **Step 4: Verify GREEN.** Run targeted tests. Build a **test wheel only**, `python -m pip wheel --no-deps -w dist-audit .`, then `python scripts/check_public_artifacts.py --wheel dist-audit/*.whl`. Expect 0 findings; keep `dist-audit/` untracked and delete build artifacts without touching the project source. (Use CI `RUNNER_TEMP` for the real job.)
- [ ] **Step 5: Commit.** `git add scripts/check_public_artifacts.py tests/test_public_artifacts.py && git commit -m "test: scan public package archives for private paths"`.

### Task 4: Draft an offline-only evidence handoff contract without an exporter

**Files:**
- Create: `docs/ip/OFFLINE-EVIDENCE-EXCHANGE-PROPOSAL.md`
- Create: `examples/evidence/synthetic-bundle-v0.json`
- Test: `tests/test_evidence_proposal.py`

**Interfaces:**
- Proposed **document and example only**; no public runtime API, no new schema constant, no CLI subcommand, no exporter and no network.
- A sample envelope uses `schema_version: "pexisos.evidence-export.proposal.v0"`, `mode: "synthetic_only"`, `purpose: "offline-contract-validation"`, a `producer` describing the `pexislink` prototype, `scope.entity_ids`, an explicit `operator_declaration` of **unverified** intent, synthetic `provenance` and a single sample from the existing `pexislink.observation.v0` vocabulary.
- A `content_sha256` is computed over ASCII bytes of `canonical_json(observations)`. It detects content changes but is not proof of source identity or legitimate data handling.

- [ ] **Step 1: Write RED fixture-contract tests.** `test_proposed_example_is_unattested_and_digest_matches` asserts `mode == "synthetic_only"`, `provenance.source == "synthetic_fixture"`, `provenance.license == "project_original"`, `operator_declaration.verification == "not_authenticated"`, `parse_observation` accepts every included raw synthetic record, scope matches the record's entity, and the SHA-256 matches. `test_tampered_copy_does_not_match_digest` mutates a payload value and requires a different digest. `test_example_has_no_remote_or_claimed_authorization` asserts no URL, credential, verified-principal claim or device command field.
- [ ] **Step 2: Verify RED.** Run `PYTHONPATH=src python -m unittest discover -s tests -p 'test_evidence_proposal.py' -v`. Expected: missing example document/file.
- [ ] **Step 3: Produce a deterministic example and draft.** Use one existing synthetic adapter observation (`pexislink.fabric.adapters.vehicle`) with its contract-valid schema and computed digest. The document states: human-selected input; bounded future exporter; source/license checks; deny on missing scope; no source data beyond explicit consent; hash is **not** a signature; no assertion of safety or authenticity; invalid/unrecognized schema/version fails closed. Document that a later exporter must operate on the **trusted operator side**, outside the request-side audit guard, and that remote HTTP/B1 remains blocked. This task must **not** implement `validate_bundle`, `export_bundle`, or any other live method.
- [ ] **Step 4: Verify GREEN.** Run targeted tests and `python -m json.tool examples/evidence/synthetic-bundle-v0.json > /dev/null`. Confirm the example includes **no real vehicle identifier, secret or client data**.
- [ ] **Step 5: Human review gate.** Request feedback on the proposed vocabulary and transfer semantics before designing any D2 implementation. The presence of the example does not authorize real-data collection or Cloud deployment.
- [ ] **Step 6: Commit.** `git add docs/ip/OFFLINE-EVIDENCE-EXCHANGE-PROPOSAL.md examples/evidence/synthetic-bundle-v0.json tests/test_evidence_proposal.py && git commit -m "docs: propose synthetic offline evidence exchange contract"`.

### Task 5: Integrate D1 checks into CI and preserve existing functionality

**Files:**
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md`
- Test: `tests/test_open_core_ci.py`

**Interfaces:**
- Consume: scripts and fixtures from Tasks 1–4; no change to `src/pexislink` or runtime.
- Produce: one additional CI job named `open-core-boundary` on Python 3.12, with read-only checkout and a test wheel in `RUNNER_TEMP`.

- [ ] **Step 1: Write a failing CI contract test.** `test_workflow_includes_d1_boundary_gates` reads `.github/workflows/tests.yml` and requires one `open-core-boundary` job with the exact commands `scripts/ip_inventory.py --check`, `scripts/check_open_core_boundary.py --check`, `scripts/check_public_artifacts.py --wheel` and the wheel build. Verify the checkout sets `persist-credentials: false` and does not grant write/secrets permissions.
- [ ] **Step 2: Verify RED.** Run `PYTHONPATH=src python -m unittest discover -s tests -p 'test_open_core_ci.py' -v`. Expected: no `open-core-boundary` job yet.
- [ ] **Step 3: Implement one isolated CI job.** Add `open-core-boundary` to `tests.yml` without editing/replacing existing jobs or their gates: use `actions/checkout@v4` (`persist-credentials: false`), `actions/setup-python@v5` (`python-version: "3.12"`), commands `python scripts/ip_inventory.py --check`, `python scripts/check_open_core_boundary.py --check`, `PYTHONPATH=src python -m unittest discover -s tests -p 'test_evidence_proposal.py' -v`, `python -m pip wheel --no-deps -w "$RUNNER_TEMP/wheel" .`, then `python scripts/check_public_artifacts.py --wheel "$RUNNER_TEMP/wheel"/*.whl`. The wheel build may obtain normal build dependencies; runtime Core remains network-free.
- [ ] **Step 4: Document the boundary.** Add one short `README.md` section linking the accepted architecture spec, IP register, script checks and **proposed, not implemented** exchange contract. Explain that green CI does not certify IP ownership, absence of all secrets, security or legal compliance.
- [ ] **Step 5: Verify GREEN locally.** Run `PYTHONPATH=scripts:src python -m unittest discover -s tests -p 'test_open_core_ci.py' -v` and `PYTHONPATH=src python -m unittest discover -s tests -v`, plus every new CLI audit. Expected: all pass, no tests skipped unexpectedly.
- [ ] **Step 6: Verify GitHub matrix on final SHA.** Open a draft PR from a clean, isolated branch; confirm all existing GitHub Actions jobs for Python 3.11–3.13, real MCP, OpenAI Agents and cantools/benchmark still pass **plus** `open-core-boundary`. Revalidate Codex compatibility if PR #13 has merged by then. Require independent review on the final commit, not an earlier HEAD.
- [ ] **Step 7: Commit.** `git add .github/workflows/tests.yml README.md tests/test_open_core_ci.py && git commit -m "ci: enforce open core boundary and public artifact checks"`.

## Final D1 Acceptance and Honest Coverage

The D1 implementation is complete only after: (1) the public inventory is structurally complete, with **ownership/legal review still explicitly pending** until separately signed off; (2) negative tests reject known private import/package mistakes; (3) a real built test wheel is inspected; (4) the evidence proposal/sample passes its synthetic-only checks; (5) every current mainline CI job stays green, plus the new boundary job; (6) independent reviewer confirms the final diff; (7) the user authorizes a merge separately.

The scanner does **not** prove that already-public Python source is secret, that arbitrary malicious obfuscation is detectable, or that Git history, external registries, future release assets and third-party licensing are fully vetted. Do not conflate mechanical checks with a legal sign-off. Keep `D1` status `review_pending` in the human register until the provenance review is actually finished.

## Later work: separate plans, not part of this execution approval

- **D2**: after approval of the example contract and privacy/security design, create a separate implementation plan for a trusted operator-side, bounded offline synthetic exporter and a mock consumer with tamper/failure/permission tests. No live devices or network.
- **D3**: only after source-ownership confirmation and separate approval, create a private Enterprise service/repository; no private source in the public tree or build.
- **D4**: remote Pexis Cloud and authenticated tenant service need separate threat model, infrastructure and legal assessment, plus explicit approval of Fabric decision B1.
- **Releases/on-premises**: separately design cryptographic provenance verification, licensing and support. No release, package obfuscation or closed client distribution is authorized by D1.

## Owner Review and Execution Gate

This document is a **plan**, not an authorization to execute. The reviewer should confirm the five deliverables, the intentionally postponed proprietary/network layers, and whether the public synthetic example exposes the right **non-authoritative** contract vocabulary. After plan approval, choose execution mode: (A) Claude Code or another independent worker, one task/independent review at a time; or (B) native execution followed by independent review of the whole branch. Either route starts with a separate isolated branch/worktree, uses TDD, and pauses at D2/D3/D4 gates.
