# PexisOS Open Core: Public Source and IP Review Register (D1)

**Status: `review_pending`. Legal signoff: `not_recorded`.** This is an *engineering inventory*, not a legal opinion or a declaration of exclusive ownership. CI only checks whether tracked files are covered by valid classification rules. Classifications are provisional, and `ownership_review: pending` applies to every rule.

## Licensing baseline

- Public PexisOS Core uses the existing Apache-2.0 `LICENSE` and Python project metadata. Existing grants are not revoked or retroactively converted into commercial licensing.
- `repository-license` identifies material *distributed in the open repository*. It does not assert that Pexis owns every contributor's copyright or that all inclusions are appropriately licensed.
- `third-party-reference` is used for dependency requirement references and lock/hashes, **not** vendor-source ownership. Confirm actual dependency licenses and notices separately.
- `review-required` marks example datasets and generated reports pending source, privacy and license validation. No claims of real-device or customer-data provenance are made.
- `generated-synthetic` is available only when synthetic origin and reuse rights have been verified. No current file gets that classification automatically.

## PexisOS Console and PETRON inventory additions (2026-10-23)

The provisional inventory now covers the already-public Console V1 tree and the PETRON
runtime expected from PRs #23 and #24 without classifying every future `apps/`
directory automatically. `apps/console/` and `apps/petron/` are
`repository-license` with `ownership_review: pending`; this records their
presence in a public source tree, **not** an opinion about copyright title.

Exceptions require a separate rights/provenance decision:

- `apps/console/package-lock.json`: `third-party-reference`; inspect actual
  dependency licenses and notices before redistribution claims.
- `apps/console/docs/console-v1-1440x900.png`: `review-required`; confirm
  image/assets ownership and any third-party design/reference content.
- `apps/petron/fixtures/` and both PETRON E001/E002 evidence directories:
  `review-required`; verify the claimed synthetic origin and that no real
  credentials, private paths or client data were included.

All classifications remain provisional. No file was granted
`ownership_review: reviewed`; `legal_signoff` remains `not_recorded`.
A clean inventory job is **not** approval to ship or merge the agent.

Before the Open Core D1 merge, run the inventory and boundary checks on the
actual combined tree including both apps and `.github/workflows/petron.yml`.
Enforce the PETRON workflow's execution and non-bypass controls, and disposition
the outstanding P1 source/artifact/CI findings in PR #21. Human licensing and
source provenance decisions remain a separate gate.

## Questions requiring human review

| Area | Evidence requested | Signoff |
| --- | --- | --- |
| Earlier Apache-2.0 grants and published versions | First-publication history; release assets and distribution records; any third-party rights | Pending |
| Contributors, employment and external collaboration | Contribution provenance, assignment/license permissions, CLA or equivalent only if applicable | Pending |
| Third-party code vs dependency metadata | Audit copied snippets, generated files, vendored code, requirement declarations and their notices | Pending |
| Dependency licenses and notices | Review direct/transitive libraries, redistribution triggers and license compatibility | Pending |
| Documentation, examples and reports | Confirm authorship, source/licenses, synthetic claims, and absence of personal or confidential material | Pending |
| New proprietary Enterprise intellectual property | Confirm original development, clear licensing rights and strict separation from already-public Core | Pending |
| Source history, CI and package exposure | Review all Git history, tags, artifacts, public registries and CI logs for sensitive content | Pending |
| Patents and trademarks | Specialist legal advice; source inventory cannot establish or grant these rights | Pending |

Do not store contributor personal data, private commercial algorithms, customer datasets, confidential agreements, credentials or legal review records in this public file. Record public outcomes only after the appropriate human reviewer approves disclosure. Any confidential evidence should be handled in a separately authorized private system.

## Mechanical gate

Run `python scripts/ip_inventory.py --check`. It requires the tracked-file inventory to be structurally complete, rejects unknown top-level tracked paths, malformed categories, unsafe paths and any ungrounded change to `legal_signoff`. This check **does not** prove title, permission, privacy compliance, lack of secrets, complete distribution coverage, or authenticity.

`python scripts/check_open_core_boundary.py --check` and `python scripts/check_public_artifacts.py` share one definition of the private names (`scripts/open_core_names.py`). Names are compared after Python packaging name normalization (case and runs of `-`, `_`, `.`), so `pexisos.enterprise`, `PexisOS.Cloud` and `pexisos__cloud` are the same project as `pexisos-enterprise`/`pexisos-cloud`. The boundary check rejects private names in any tracked path segment (package directory, single-file module, compiled extension, `.pth`, versioned metadata), tracked gitlinks and symlinks, private imports (static and literal dynamic, including `name=`/`package=` forms) in every tracked Python file, and private references in build requirements, dependencies, extras, dependency groups, scripts, GUI scripts, entry points, any `tool.*` table and the files read by setuptools dynamic metadata (an unreadable, escaping or symlinked file fails closed). The artifact check applies the same name rules to every member path of a wheel or sdist (the sdist root is never stripped) and reads the requirement fields of `METADATA`/`PKG-INFO`, `entry_points.txt` and `requires.txt`, within archive size, member-count and metadata-size limits; member names are escaped in diagnostics. These are tripwires: computed imports, renamed copies of private code, file bodies other than packaging metadata, and Git history are not inspected.

Implementation of Cloud, Enterprise services, licensing enforcement, network export, real devices or production release requires separate authorization. The Core remains functional offline and the existing `pexislink` runtime is unaffected by this tooling.

## Brain visual assets (2026-10-23)

The PETRON Brain image and alpha mask (`apps/petron/brain/brain-texture.webp`,
`apps/petron/brain/brain-mask.webp`) are explicitly `review-required`.
Visual acceptance of a prototype is not proof of copyright or redistribution
rights. Check provenance and licenses before public release. Source code remains
provisionally `repository-license`; `ownership_review: pending` and
`legal_signoff: not_recorded` remain unchanged.
