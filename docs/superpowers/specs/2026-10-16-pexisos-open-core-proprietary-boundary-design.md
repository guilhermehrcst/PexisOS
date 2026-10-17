# PexisOS open-core and proprietary execution boundary

**Status:** proposed architecture, awaiting owner review of this written specification  
**Date:** 2026-10-16  
**Scope:** public, high-level architectural boundaries only; no proprietary source, trade secrets, credentials or operational defensive details  
**Baseline:** PexisOS main at 289bc0173d28aeaa5b00b46bb1ae4d6d7a588658

## 1. Intent and decision

PexisOS will follow an **open-core** architecture. Its existing permissively licensed, useful and provider-neutral core remains open source and can run offline without a subscription. Distinct, **newly developed commercial capabilities** may live in private source repositories and be executed as separately operated services. The first commercial execution mode will be **private server-side**, not code distributed to customer machines.

The goal is to protect newly created intellectual property while preserving public auditability, interoperability and the project's ability to attract independent developers, researchers and institutions. The objective is **not** to claim that reverse engineering can be prevented, nor to make already published source confidential.

Owner-approved direction before this document: open core remains openly usable; commercial logic is kept private; server-side execution is preferred initially; any future on-premises edition is a separate decision.

## 2. Current baseline, not promised future behavior

- The public repository is guilhermehrcst/PexisOS, with an Apache-2.0 LICENSE and Python package, commands and schema namespaces still named **pexislink**. The naming migration is a separate initiative.
- Existing public functionality includes offline telemetry research, v0.4 file-to-file CAN/DBC replay, and an experimental read-only Agent Fabric with identity binding by its trusted process launcher, per-entity capability checks, audit records and an optional MCP **stdio** adapter.
- The Agent Fabric does **not** currently provide cryptographic end-user authentication or an approved remote HTTP transport. These are explicit boundaries, not implementation gaps to bypass.
- The project's Python audit hooks are defense-in-depth tripwires, not an OS security sandbox. Do not treat them as a confinement guarantee against hostile native code.
- Real devices, writable/actuating endpoints, remote MCP and non-synthetic datasets require separately approved designs and safety/privacy reviews.
- The multi-provider compatibility lab (PR #13) and its v0.2 contract **proposal** are independent ongoing work. This document does not approve merging that PR or turning the proposal into a new runtime API.

## 3. Considered approaches

| Approach | Intellectual-property protection | Cost to openness and operations | Decision |
| --- | --- | --- | --- |
| Obfuscate or compile the distributed Python core | Adds effort but source is already public; distributed binaries remain analyzable | Reduces auditability and may hurt reproducibility | Reject as primary control |
| Turn the entire product private | Cannot revoke previously issued Apache-2.0 rights | Breaks the developer/research strategy | Reject |
| Open core plus distinct private commercial service | Server-side source and algorithms are not distributed to clients | Requires disciplined interfaces, access controls and service operation | **Selected** |

Obfuscation may later be evaluated for an **optional proprietary on-premises** component, but cannot replace deployment isolation, a sound license, identity controls or a security review.

## 4. Ownership, license and dependency direction

| Layer | Source and license boundary | Responsibility | Allowed dependency direction |
| --- | --- | --- | --- |
| **PexisOS Core** | Public repository, Apache-2.0 | Read-only Agent Fabric, contracts, policy, audit, synthetic adapters, MCP stdio, offline evidence tooling and tests | Depends only on approved open dependencies; **never** imports, loads or requires Enterprise or Cloud |
| **PexisOS Enterprise** *(future)* | Separate private repository, with an explicitly reviewed commercial license | New proprietary algorithms, commercial analysis and enterprise integration logic | May consume versioned public Core contracts or published packages; cannot modify or bypass Core policy as a privileged backdoor |
| **Pexis Cloud** *(future)* | Private service implementation and operations | Managed tenancy, service authentication, storage, billing where later approved, jobs, monitoring, lifecycle | Calls Enterprise via internal least-privilege interfaces; provides a **separate** public-facing service boundary, not implicit network reachability from Core |

A customer must still be able to run the documented Core demo, offline CAN lab and local read-only MCP transport **without** an Enterprise or Cloud account. No subscription checks, call-home behavior or commercial access gate may be inserted into those existing open paths.

Open-source contracts, example payloads and consumer conformance tests must remain public; secrets, customer datasets, proprietary methods and private service topology do not belong in the public repository, its Git history, CI logs, PR comments, artifacts or package distributions.

## 5. Reference data flow and deployment boundaries

**Phase-one proof is intentionally offline and manual.** The following interfaces are proposed; they do not exist yet:

1. A trusted **human/operator** works with synthetic observations from the existing local Core/Lab A. Session requests remain read-only and do not gain file export, network or device powers.
2. An **optional, separately invoked operator-side exporter** (future open implementation, outside request-handling guards) accepts only explicitly named local inputs and explicit scope and produces a bounded, versioned evidence bundle. It does not start by default and performs no network operation.
3. The operator explicitly elects to hand off that file to a separate private Enterprise research service. For the first experiment, this handoff can be manual/local. There is **no** HTTP endpoint added to Core, no embedded endpoint, and no silent telemetry export.
4. The Enterprise research service validates the bundle's schema, size, provenance claims and purpose restrictions **before** invoking private analysis. Its new algorithms stay server-side and are never packaged with Core.
5. The service returns a separate **inference result**, linked to the source bundle digest and analysis/version metadata. Results must not be presented as measurements, diagnoses, hardware control decisions or certified safety findings.
6. The operator controls any later import/display of an inference. A later automated synchronization or Cloud gateway would need its own authentication, authorization, transport, retention and security design.

Reference flow:

    Synthetic data -> existing offline Core / Lab A
                         |
                         | operator-approved, local file input only
                         v
                   OPTIONAL exporter (future; public)
                         |
                         v
                  Evidence bundle (bounded file)
                         |
                    explicit manual transfer
                         |
                         v
                Private Enterprise service (future)
                         |
                         v
                  Separate inference result

**Isolation property:** proprietary service failures, unavailability or subscription state must not change the deterministic outputs or functional availability of the open Core.

## 6. Proposed cross-boundary contract, without freezing a new API

The exporter and commercial consumer will later agree on a small, public, versioned envelope. The future schema name and exact fields require a separate contract review; this section is a semantic specification, **not** an implemented or stable schema.

**Evidence bundle minimum meaning:**
- A schema version and bounded payload size/record count.
- An operator-provided purpose and explicit export authorization event, distinct from a model-generated instruction.
- The producer name/version, input file digest(s), source/license/consent metadata, and an immutable content digest.
- Observation timestamps, entity identifiers scoped to the authorized export, quality/freshness markers, and explicit missing/unknown values. Minimize identifiers and strip unrelated private fields.
- No implicit claim that an ordinary SHA-256 digest authenticates the producer. Cryptographic origin attestation, if needed, requires a real signer identity and trust policy.

**Inference result minimum meaning:**
- Link to the validated bundle digest and tenant/job identity in a private service.
- Method identifier/version and explicit distinction between direct evidence, derived inference, insufficient evidence and unsupported claims.
- Processing timestamp, bounded outputs and a privacy-safe audit identifier. Never assert a failure probability or physical safety conclusion without separate scientific validation.
- Sanitized errors and fail-closed outcomes for invalid bundle, revoked permission, unknown schema/version, unavailable analysis or exceeded resource limits.

Core's existing tool schemas and current read-only MCP behavior must remain byte-compatible unless a *separate* reviewed versioning proposal approves changes. A private service cannot automatically acquire the trusted Core operator role.

## 7. Trust boundaries, non-negotiable controls

1. **Principal authority.** Existing Core permissions are enforced in the open Core. Private services must not infer authorization solely from a client-supplied principal string, bundle field or model output. An external service requires genuine authenticated tenant/operator identity before access.
2. **Separate processes and repositories.** Proprietary code is never imported into the Core runtime or compiled into its public wheel/container. Privilege, filesystem and process isolation must be enforced by the OS/platform where appropriate. Python audit hooks are not a substitute.
3. **Network.** No existing Core/Lab A/stdio process gains outbound requests, hosted connectors or remote MCP. Any private Cloud gateway requires explicit subsequent approval of TLS, authentication, tenant authorization, abuse controls and exposure model (Agent Fabric blocked item B1).
4. **Data lifecycle.** The initial proof uses only synthetic data. For customer or real-device data, require prior lawful-use/licensing assessment, consent/authority, a retention and deletion policy, tenant segregation, access logs, encrypted transport/storage and a separate risk assessment.
5. **Secrets and supply chain.** No hardcoded tokens, API keys, proprietary source or client secrets in publicly built assets. Private build runners, secrets and logs must be scoped to their repository/service; verify that public builds do not fetch or embed private dependencies.
6. **Integrity and provenance.** Versioned builds, pinned dependencies, limited publish permissions, release hashes and verified artifact attestations should be adopted when shipping distributables. An attestation proves provenance, not product security.
7. **Failure behavior.** Fail closed for invalid/unsupported exports and unauthorized commercial reads. Never silently fall back to sending raw additional data or claiming success from a failed private analysis.

## 8. Intellectual property and license governance

- The public source and already published revisions licensed under Apache-2.0 remain available under the rights already granted. This decision does not purport to retract them, or retroactively change the license of others' contributions.
- Future **new and separately owned** Enterprise source can be kept private and subject to a commercial license. A legal review must check ownership, third-party dependencies, copyright notices, patent clauses and distributions that incorporate Apache-licensed components.
- Maintain an attribution/license inventory for source, vendored dependencies, releases, NOTICE requirements where applicable and original contributor provenance; changes to contribution policies need explicit review.
- Software copyright, service access conditions, registered trademarks and patent rights are distinct matters. The Apache-2.0 license does not confer general trademark-use permission. Search/registration strategies for PexisOS/Pexis require professional assessment, including the relevant jurisdiction.
- Avoid promising that an offline proprietary binary cannot be reverse engineered. **Not distributing critical server-side source** is the principal confidentiality control.
- This public document intentionally omits algorithms, secret deployment configuration, production topology, private business rules, cryptographic key material and commercial pricing.

Reference texts: https://www.apache.org/licenses/LICENSE-2.0.html ; https://www.apache.org/foundation/license-faq.html ; https://docs.github.com/en/actions/concepts/security/artifact-attestations .

## 9. Acceptance criteria for subsequent implementation proposals

The following are **gates for future PRs**, not claims about currently working code:

| Gate | What must be demonstrable |
| --- | --- |
| AC-01: Offline Core | Core's current CLI, deterministic Lab A, synthetic Fabric and stdio MCP run without Enterprise, Cloud, login, network or account service |
| AC-02: One-way code dependency | Static checks prove open Core does not import or package private Enterprise modules; private clients consume the open versioned interface |
| AC-03: Zero private leakage | Public source, commit tree, source/wheel/container distributions, tests, logs and artifacts contain no Enterprise source or secrets; repository separation independently reviewed |
| AC-04: Consent and minimization | Synthetic operator-side export requires explicit opt-in, bounded record set, declared purpose, source/license fields and no network side effects |
| AC-05: Integrity and truthful results | Content hashes verified, unknown/malformed bundles rejected, tampering tested, inferences labeled and linked to evidence, absent provenance does not become verified authenticity |
| AC-06: Failure isolation | The open Core behaves identically when Enterprise is unreachable, unconfigured, unauthorized or malfunctioning |
| AC-07: Permission and privacy | A separate threat model and negative tests precede any remote Cloud, customer dataset or multi-tenant deployment |
| AC-08: Release provenance | Released artifacts carry verifiable source/build provenance when the supported distribution pipeline is introduced; verification is exercised, not just generation |
| AC-09: Existing CI | All current Core and compatibility suites remain green at every implementation milestone; new tests check both allowed and forbidden dependency/data flows |
| AC-10: Human review | Independent review verifies the final commit for each security-relevant implementation. CI green alone is not security approval |

## 10. Rollout and explicit future gates

**D0 - Architecture review (this document).** Agree on open/private ownership, the operator-export trust boundary and non-goals. This spec adds no code, remote service, repository, deployment, product license enforcement or new API.

**D1 - IP/source audit and implementation plan (next, only after written-spec approval).** Inventory preexisting Apache files and other licenses, authorship and external contributions; identify *new* proprietary candidates without exposing details publicly. Specify standalone Core test gates and a simple public export interface. Get a commercial/IP counsel review before selling/distributing a mixed-license package.

**D2 - Synthetic-only offline contract proof (separate scoped approval and PR).** Public fixture/envelope validation and a minimal optional operator-side export; use a mock external consumer. Reproduce negative permission, tamper, size and no-network scenarios. No live hardware, no online service.

**D3 - Private Enterprise proof (separate approval, private workspace).** Only after IP inventory and contract approval, introduce a *newly created* proprietary service consuming synthetic bundles. Validate that private implementation and outputs cannot appear in public artifacts. Do not create a private repository until separately authorized.

**D4 - Optional Pexis Cloud gateway (separate architecture and security approval).** Add genuine tenant authentication, TLS, transport, service separation, lawful data treatment and recovery/retention controls. Do not silently turn on hosted MCP or remote hardware access.

Any future on-premises proprietary binary is a distinct product and threat-model decision. It is deliberately excluded from D1-D3.

## 11. Success and risk register

**First measurable success:** An independent developer can use PexisOS Core offline and inspect the public evidence contract; separately, a private synthetic-data consumer can process an explicitly transferred sample and return a clearly labeled inference without any private source entering the open package.

**Main risks and treatments:**
- Core becomes dependent on paid services: enforce AC-01 and AC-06 in CI, prohibit call-home/entitlement checks on Core.
- Proprietary logic accidentally published: restricted repositories/build separation and AC-02/03; assume any committed public secret must be rotated and any published code can be copied.
- Open source or third-party license mishandled: per-component inventory and counsel review before distribution.
- Export disguises authorization or authenticity: source/consent validation plus explicit distinction between hashing and signing; future authenticated identity.
- Remote exposure quietly expands risk: require explicit B1 approval and threat model before D4.
- Experimental telemetry presented as diagnosis: preserve evidence qualification and scientific disclaimers in all outputs.

## 12. Decision boundary for owner review

Approving this **written specification** authorizes preparation of a detailed implementation plan only. It does **not** authorize changes to the public Core, creation of private repositories, cloud deployment, new APIs, real telemetry collection, fees/license enforcement, release publication or merges to main. The implementation plan must specify ordered files, tests, ownership review and separate deployment/security gates before any implementation is approved.
