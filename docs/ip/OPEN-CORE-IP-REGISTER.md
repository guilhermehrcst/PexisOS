# PexisOS Open Core: Public Source and IP Review Register (D1)

**Status: `review_pending`. Legal signoff: `not_recorded`.** This is an *engineering inventory*, not a legal opinion or a declaration of exclusive ownership. CI only checks whether tracked files are covered by valid classification rules. Classifications are provisional, and `ownership_review: pending` applies to every rule.

## Licensing baseline

- Public PexisOS Core uses the existing Apache-2.0 `LICENSE` and Python project metadata. Existing grants are not revoked or retroactively converted into commercial licensing.
- `repository-license` identifies material *distributed in the open repository*. It does not assert that Pexis owns every contributor's copyright or that all inclusions are appropriately licensed.
- `third-party-reference` is used for dependency requirement references and lock/hashes, **not** vendor-source ownership. Confirm actual dependency licenses and notices separately.
- `review-required` marks example datasets and generated reports pending source, privacy and license validation. No claims of real-device or customer-data provenance are made.
- `generated-synthetic` is available only when synthetic origin and reuse rights have been verified. No current file gets that classification automatically.

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

Implementation of Cloud, Enterprise services, licensing enforcement, network export, real devices or production release requires separate authorization. The Core remains functional offline and the existing `pexislink` runtime is unaffected by this tooling.
