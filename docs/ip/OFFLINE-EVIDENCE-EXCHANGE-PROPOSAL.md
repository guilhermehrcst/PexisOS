# PexisOS D1: Offline Evidence Exchange (Proposal Only)

**Status: D1 proposal, not implemented.** This document describes a possible future operator-authorized, offline, synthetic-data handoff between the Apache-2.0 public Core and a separate hypothetical private consumer. The proposed envelope is **not** a stable public API, identity document, signature, access credential or authorization token. No exporter, importer, remote endpoint, network transport, authentication mechanism, Cloud service, enterprise algorithm, device connection or telemetry collection is implemented by D1.

## Separation and threat boundaries

1. **Trusted operator side (future D2, not implemented):** a human selects named local synthetic input files, explicit entity IDs, a narrowly defined purpose and scope. A proposed, separately launched exporter checks the input schema, provenance and license, refuses unsupported sources, enforces size/record/time limits and records the operator's declared consent. There must be no default or background collection, no implicit collection from the Fabric memory, and no network access. The exporter must not run inside the request-side read-only MCP policy guard or inherit its authority.
2. **Human-controlled manual file transfer (future):** the operator may elect to transfer the file offline into a separately trusted analysis context. The Core never calls a private service on its own. A SHA-256 content digest is **not an authorization** or proof of producer identity, consent, authenticity or trustworthy origin; changing the file and recomputing the hash remains possible.
3. **Untrusted consumer (future D2):** a mock consumer would reject unknown versions, invalid/oversize payloads, unmatched entity scope, missing provenance and malformed records before doing any analysis. A future commercial consumer would additionally require genuine independently authenticated operator/tenant identity, explicit authorization, data minimization, audit, and separately approved retention and deletion policies. Operator-provided strings are assertions, never verification.
4. **Safety:** evidence and private inferences must be explicitly distinguished. Neither a synthetic CAN signal nor an algorithm output certifies real hardware condition, predicts physical failure or authorizes any device actuation.

## Draft envelope vocabulary (non-stable)

The sole example at `examples/evidence/synthetic-bundle-v0.json` declares `schema_version: pexisos.evidence-export.proposal.v0`, `mode: synthetic_only`, `purpose: offline-contract-validation`, a `producer` descriptor, explicit `scope.entity_ids`, `operator_declaration` with `verification: not_authenticated`, provenance from `synthetic_fixture` and `project_original`, one observation valid under the **existing** `pexislink.observation.v0` vocabulary, and `content_sha256`. The proposal deliberately adds no constants or runtime types to `src/pexislink`.

For deterministic test data, `content_sha256 = SHA256(ASCII(canonical_json(observations)))`, where `canonical_json` uses sorted keys, compact separators, `ensure_ascii=True`, and disallows NaN. This detects modifications when compared with a separately trusted expected digest. It is neither a digital signature nor source attestation. An attacker can recompute both content and digest. The example declares `not_authenticated` and carries no verified principal, credential or endpoint.

## D2 design work requiring separate approval

- Define strict envelope field allowlists, maximum file/record/string sizes, explicit consent UX, portable source/license records, freshness and timestamp verification, and consistent failure codes. **Fail closed** on absent or ambiguous scope and on unknown schema/version; never broaden the authorized selection.
- Define a trusted operator-owned export mechanism **separate from the request-side MCP/read-only audit guard**, with no network and no access to private service keys. Validate with entirely synthetic inputs and local mock consumers.
- Decide whether cryptographic attestation is necessary. If yes, separately design signer identity, private-key custody, chain of trust, key rotation and replay prevention. A bare SHA-256 value is not enough.
- Decide whether an eventual authenticated remote boundary is ever needed. It requires a separate B1 decision, security/privacy threat model and authorization. This proposal does not permit HTTP/SSE/remote MCP or Cloud deployment.

**Explicit exclusions:** real device signals, customers' data, credentials, telemetry upload, background collection, any live exporter, proprietary implementation, license enforcement, production release, or modifications to existing Core CLI/API/contract behavior. Legal provenance and licensing remain subject to human review (`OPEN-CORE-IP-REGISTER.md`).
