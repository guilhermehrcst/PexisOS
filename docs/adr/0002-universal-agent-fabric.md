# ADR 0002 (Proposed): Provider-neutral PexisLink Agent/Device Infrastructure

- **Status:** PROPOSED, pending owner and engineering review. The overall direction is still a proposal. A first read-only slice is now **implemented** and merged; see §0 for what is IMPLEMENTED, PROPOSED or BLOCKED.
- **Date:** 2026-10-15 (reconciled with the merged code on 2026-10-16)
- **Tracking issue:** [#8](https://github.com/guilhermehrcst/Pexislink/issues/8)
- **Scope:** long-term product architecture only. This ADR changes **no** code, permissions, deployment or existing release behavior.

## 0. Implementation status (reconciled 2026-10-16)

This ADR was written before any code existed. Since then:
- **Agent Fabric v0.1** was merged in PR #11 (`f7c6a7a`);
- the optional **OpenAI Agents SDK compatibility suite** was merged in PR #12 (`3396864`).

The authoritative description of what exists is the code in `src/pexislink/fabric/`, plus these documents:
- [AGENT-FABRIC.md](../AGENT-FABRIC.md)
- [AGENT-FABRIC-THREAT-MODEL.md](../AGENT-FABRIC-THREAT-MODEL.md)
- [AGENT-FABRIC-DECISIONS.md](../AGENT-FABRIC-DECISIONS.md)
- [COMPAT-OPENAI-AGENTS.md](../COMPAT-OPENAI-AGENTS.md)

The vision in §1–§8 is unchanged. Where the implemented contract differs from the illustrative text below, a **Reconciliation note** marks the difference, and the code wins.

### IMPLEMENTED (in `main`, covered by tests and CI)

| Area | What exists | Evidence |
|---|---|---|
| Placement | One local process, standard library only. The core imports no model or provider SDK, no MCP SDK and no cantools. | `tests/test_architecture.py` (`AgentFabricIsolationTests`); compat T15 |
| Entities | `EntitySpec` (`pexislink.entity.v0`): synthetic, operator-registered identifiers (`namespace:kind:name`), typed signals with units, plausible ranges bounded to ±1e15, enum vocabularies, and timing (`expected_interval_s`, `max_latency_s`, `stale_after_s`). There is no attestation. | `contracts.py`; `test_fabric_contracts.py` |
| Observations | `pexislink.observation.v0`, closed schema: `schema_version`, `event_id`, `entity_id`, `observed_at`, `received_at` (RFC 3339, `Z` only), `source` (must equal the entity's adapter), `kind`, `payload` (declared signals only; strings only as enum values), `quality`, `provenance`. | `contracts.py`, `store.py`; contract and adversarial tests |
| Evidence states | `observed`, `late` (derived by the core only), `unknown`, `rejected` (counted, never stored) and `insufficient`. Assessment states are `nominal`, `deviation`, `stale`, `insufficient` and `unknown`. Freshness follows usable evidence, per signal. A numerically unsafe computation abstains and is never `nominal`. | `assess.py`; `test_fabric_final_review.py` |
| Capabilities | Exactly four read-only capabilities: `observe`, `describe`, `read_history` and `assess`. Any other name, such as `transmit`, `write`, `install`, `scan` or `enroll`, cannot be represented and fails closed as `capability_not_supported`. *Declared* (supported), *available*, *authorized* and *executed* are reported separately. | `contracts.py`, `runtime.py`; T10 and T11 in the threat model |
| Policy | Deny by default. Exact per-principal, per-entity, per-capability grants with a **mandatory** expiry, immediate revocation and an injectable clock. Every internal fault fails closed. An entity the principal holds no grant for answers `not_found`, the same as an absent one. | `policy.py`; `test_fabric_policy.py` |
| Identity of the caller | The principal is **assigned by the operator when the session or server starts** (`Fabric.open_session`, or `--principal` for stdio). It is never taken from tool arguments. **There is no cryptographic client authentication**: the trust root is whoever launches the process. | `runtime.py`, `mcp_server.py`; compat T05 |
| Audit | A bounded, hash-chained, in-memory log. Every attempt leaves a record with an explicit event: `rejected`, `authorization`, `failed` or `completed`. `executed: true` is written only after every final check. A record is committed only after its sink accepts it, and a failing sink fails the call closed. Records hold codes, never payload values or raw arguments. | `policy.py`, `runtime.py`; `AuditSemanticsTests` |
| Provenance | `source: synthetic_fixture`, `license: project_original`, `generator`, `seed`. Only synthetic data exists. | `contracts.py` |
| Domain adapters | Three synthetic domains share one contract: a refrigerator, a CAN vehicle built on a test signal table (unknown frames carry an empty payload) and a software service. | `adapters/`; `test_fabric_runtime.py` |
| MCP transport | Optional, using the official Python SDK (`mcp==2.2.0`, low-level `Server`), **stdio only**. Five tools: `list_entities`, `describe_entity`, `list_capabilities`, `read_observations` and `explain_assessment`, each with a closed input schema whose parity with the core validator is tested. The transport only calls `Session.call`. | `mcp_server.py`; `test_fabric_mcp.py` |
| Provider compatibility | The published OpenAI Agents SDK (`MCPServerStdio`) passes the 15-case matrix on Python 3.11–3.13, with no model, API key or network. Interoperability findings I1–I6 are documented. | PR #12; `reports/compat/` |
| Determinism and bounds | Byte-identical demo and replay. Limits on entities, observations per entity, page size, result bytes, payload depth, keys and string length, audit records and grants. Measured resource use is documented. | `AGENT-FABRIC.md` §9–10 |

> **The MCP annotation `readOnlyHint` is not a security boundary.** It is advice to the client. Isolation comes from the absence of any write capability in the contract, the server-side policy and the `no_effects()` audit-hook tripwire. That tripwire is not a sandbox either: native code can bypass Python audit hooks.

### PROPOSED (direction only; no code)

- On-device agents, local gateways representing several devices, and authorized remote connectors (§2 *Execution placements*). Only a single local process exists today.
- A `recommend` capability. `replay` exists as a determinism property of the store and the demo, not as a separate capability.
- Wrapping the v0.4 CAN replay lab (PR #10) output as a Fabric adapter. The Fabric's vehicle adapter is currently independent and synthetic.
- Compatibility gates for Claude Code, Codex CLI and the Claude Agent SDK as MCP clients.
- **Contracts v0.2:** a possible split of `read_observations` into `read_latest` / `read_history` to resolve interoperability finding I1. This is a migration proposal; the existing tool is neither removed nor renamed.
- Promotion of the `v0` schemas to a stable public API.

### BLOCKED (require explicit owner authorization and a separate validation)

These are decisions B1–B6 in AGENT-FABRIC-DECISIONS.md:
- any network transport (HTTP or SSE), hosted connectors or remote clients;
- any non-read-only capability: write, transmit, control, install, execute, scan or enroll;
- real devices, buses or services;
- cryptographic entity or principal identity;
- persistent storage;
- a stable public API.

Actuator control (§4) remains out of scope indefinitely.

## 1. Why this exists

PexisLink started as **offline vehicle telemetry and anomaly research**. The long-term goal is **not to build a consumer app or a competing AI assistant**. It is to become a **vendor-neutral, open-source infrastructure component** that developers and potentially AI labs such as OpenAI and Anthropic can adopt to connect their own models and agents to authorized physical and digital capabilities.

Adoption by OpenAI, Anthropic or any other organization is **an aspiration, not an existing partnership, endorsement, integration contract, or commitment**. First earn that possibility with reliable code, security, interoperability, tests, open governance and upstream contributions.

The proposed long-term ambition is a **permissioned agent fabric** in which heterogeneous *physical and digital entities* can be represented by agents that observe authorized signals, keep bounded context and assist their owners. This includes vehicles, appliances, robots, factory equipment, satellites (future separately qualified research), computers, mobile applications, servers, business services and software workflows.

**"An agent for everything" is not "install an LLM on everything".** An entity can have a small deterministic agent, an optional local model, or an agent hosted on an owner-controlled gateway/remote system. Some entities cannot run code or expose telemetry at all; integrations exist only when documented interfaces and owner authorization are available.

This is **not** a self-replicating AI, unauthorized device discovery system, malware-style persistence mechanism, universal remote controller or safety controller.

## 2. Decision proposal

Retain **PexisLink** as product identity. Use **PexisLink Everywhere** for the direction and **Agent Fabric** as the technical layer. Evolve by adding an optional, **offline-first** horizontal boundary, not by rewiring the existing vehicle detector.

The core abstractions should be independent from a specific transport, hardware vendor, machine-learning library, foundation model or cloud provider.

### Separation of concepts

| Concept | Responsibility | Important limitation |
|---|---|---|
| Entity | Pseudonymous owner-scoped identity for a machine, software service, process or physical asset | No claim of hardware attestation merely from a user-supplied ID |
| Agent | Bounded component representing an entity, analyzing permitted observations and emitting assessments/recommendations | Does not need an LLM or run on that physical entity |
| Adapter | Converts data from one *explicitly enrolled* source into validated observations | No unapproved scanning, networking or command transmission |
| Capability manifest | Advertises what is supported, read-only by default, with versioned schemas | Advertising support does not authorize execution |
| Policy | Explicitly grants limited actions by principal/entity/capability, with expiry/revocation | Deny by default; no automatic elevation from model outputs |
| Memory/state | Per-entity historical observations and compact derived state, with retention limits | No global free-form cross-device memory by default |
| Intelligence | Deterministic rules first; optional small local or remote models behind the same interface | Model text is untrusted data, not privileged instructions |
| Audit | Records authorized enrollment, data origin, decisions and approved actions | No passwords, raw personal content or location traces in logs |

### Execution placements (same contract)

1. **On-device:** a small agent when the user controls a capable host. Budget CPU, RAM, battery and latency; do not assume GPUs.
2. **Local gateway:** a user-controlled process represents several sensors or closed appliances; the appliance may have no agent binary.
3. **Authorized remote connector:** used only with explicit opt-in, credential scoping, transport encryption, limited retention and a documented disconnect mechanism.

Placement is a deployment detail. **Entity identity and authorization cannot be inferred from where the agent runs.**

### Developer-first infrastructure, not a PexisLink consumer product

**Primary users are application, device, infrastructure and agent-framework developers**. The deliverables are reusable libraries, a specification, permissively usable local adapters, a reference MCP server, integration examples and conformance tests. An app with a PexisLink logo is **not** the intended product. End-user control may be supplied by Claude, ChatGPT, an OEM's interface or the customer's own application.

The **Model Context Protocol (MCP)** already standardizes how AI applications access tools and context. OpenAI's Agents SDK/Responses API and Anthropic's Claude API support MCP servers; see:

- https://openai.github.io/openai-agents-python/mcp/
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp
- https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- https://modelcontextprotocol.io/

**Do not fork or replace MCP.** The missing specialization PexisLink would explore is an **edge/physical-world capability substrate** behind MCP: identity and provenance for physical/digital entities, typed observations, permissions, bounded local execution, interoperability adapters, reliable abstention/stale-data semantics and audit. MCP is the external model-facing interface; PexisLink's **internal API is model/provider-independent**. An optional later A2A adapter is justified only if real cross-agent delegation is required, not as MVP scaffolding.

```text
  Claude / OpenAI agents / other model and app clients
                       |
              standard MCP tool API
                       |
            PexisLink MCP adapter
                       |
          identity + scoped policy gate
                       |
       typed observations + local audit
                       |
  explicitly enrolled, read-only domain adapters
    (synthetic sensors / recorded CAN / local service)
```

**First minimal read-only MCP tool candidates** (names are provisional; freeze only after conformance review):

> **Reconciliation note (IMPLEMENTED).** All five tools exist, **without** the `pexislink.` prefix, as `list_entities`, `describe_entity`, `read_observations`, `explain_assessment` and `list_capabilities`. When an MCP client connects more than one PexisLink server, name collisions are handled on the client side, for example with the OpenAI Agents SDK's `include_server_in_tool_names` (finding I4). `read_observations` takes `mode=latest` (capability `observe`) or `mode=history` (capability `read_history`, paginated by an opaque numeric cursor).

- \`pexislink.list_entities\`: list enrolled pseudonymous entities visible to the caller, with pagination and no network discovery.
- \`pexislink.describe_entity\`: return declared sensor/capability schemas and last-observed timestamps, never manufacture real-time status.
- \`pexislink.read_observations\`: read a bounded window of authenticated or synthetic observations for one authorized entity; surface provenance, freshness and quality.
- \`pexislink.explain_assessment\`: retrieve a bounded, existing evidence-backed interpretation or abstention; do not add an unvalidated diagnosis.
- \`pexislink.list_capabilities\`: report supported/authorized read-only capabilities; support is not permission, and there are no write/actuator capabilities in the MVP.

**Provider compatibility:** test the same MCP server with independent open-source MCP clients and the relevant OpenAI/Anthropic SDK adapters. These tests can validate protocol compatibility **without paid inference or claiming corporate adoption**. Test a real API integration only when the owner supplies authorization/credentials and expressly approves external data flow; do not publish any secret. Anthropic's hosted connector currently requires a reachable remote HTTP server for direct Messages API use, so start with local protocol/SDK conformance and do not expose a private device gateway to the public internet as a shortcut.

**Mobile is downstream.** Third parties may create mobile control applications backed by these contracts, including an authorized owner's single-phone view. An official PexisLink Mobile app, consumer dashboard, chat companion, SaaS account system and app-store launch are **removed from the core roadmap**. A minimal developer diagnostics UI is acceptable if indispensable for debugging.

### Evidence required for meaningful ecosystem adoption

1. A small, well-documented interface that solves a **specific problem MCP alone does not solve**, first with repeatable heterogeneous synthetic fixtures.
2. Portable CPU-only installation, bounded memory and predictable latency; no surprise cloud/network access or heavy model dependency.
3. Fail-closed capability enforcement and tests for revoked, expired, unknown or cross-entity access, including prompt-injection attempts to escalate permissions.
4. Versioned schemas, reproducible examples, pinned integration test matrix for OpenAI/Anthropic MCP-compatible clients and clear transitive-license documentation.
5. Public independent review, responsible vulnerability disclosure, independent maintainers/contributors and a credible migration/deprecation policy.
6. Only then engage AI labs, SDK maintainers and standards communities with data and an upstream-ready contribution. There is no guaranteed adoption or funding.


## 3. Minimum event contract (proposed, not yet an implemented API)

> **Reconciliation note (IMPLEMENTED, superseding the example below).** The implemented `pexislink.observation.v0` contract differs from the example as follows:
> - The version field is `schema_version`, not `schema`.
> - The producing adapter is `source`, not `adapter`, and it must equal the adapter the entity is registered with.
> - Timestamps accept only `Z` (UTC), not an arbitrary explicit offset.
> - `quality.status` may be claimed by an adapter only as `observed` or `unknown`. `late` and the timing flags are derived by the core.
> - Duplicate `event_id`s within the retention window are **rejected**, not accepted. Out-of-order and delayed events are stored but classified `late`. Arrival order is preserved.
> - Provenance has `source`, `license`, `generator` and an optional `seed`.
> - The assessment's abstention outcome is `insufficient` (with `stale` and `unknown`), rather than `insufficient_evidence`.
>
> See AGENT-FABRIC.md §7.

Prefer versioned JSON-compatible schemas for the first prototype; retain domain-specific typed payloads rather than forcing all observations into one generic float.

```json
{
  "schema": "pexislink.observation.v0",
  "event_id": "synthetic-event-0001",
  "entity_id": "demo:appliance:fridge-01",
  "observed_at": "2026-10-15T13:00:00Z",
  "received_at": "2026-10-15T13:00:01Z",
  "adapter": "synthetic:appliance-v0",
  "kind": "temperature_observation",
  "payload": {"temperature_c": 4.1},
  "quality": {"status": "observed", "flags": []},
  "provenance": {"source": "synthetic_fixture", "license": "project_original"}
}
```

- Strict schema version; closed required fields; bounded UTF-8/JSON size and nesting; timestamps with explicit offset; no non-finite numbers.
- Entity IDs are **not** VINs, serial numbers, account IDs or inferred real-world identities in the MVP. The namespace belongs to an explicit local user/project.
- Accept duplicate, delayed or out-of-order events without silently reordering or rewriting history; classify them and preserve raw order in replay.
- Unknown adapters or payload schemas do not get silently coerced into `normal`.
- Event provenance describes a claim and its verification status; never label a synthetic or user-supplied event as independently verified.
- Output assessments are separate, versioned records with `insufficient_evidence` as a first-class outcome. Do not call any health or safety property `normal` unless an appropriate validated reference exists.
- No raw values, full file paths, tokens, home addresses, customer identifiers or private message bodies in generic audit messages.

This is an **illustrative proposed shape**. Implementing a final schema requires tests and a dedicated review.

## 4. Security, trust and control

**No autonomous enrollment or spread.** A user/admin must authorize each integration. A newly encountered IP, Bluetooth MAC, USB device, cloud service or application does not implicitly create an enrolled agent.

MVP capabilities: `observe`, `replay`, `assess`, `recommend` on **synthetic local files only**. There are **no operational device commands**.

> **Reconciliation note (IMPLEMENTED).** The implemented read-only set is `observe`, `describe`, `read_history` and `assess`. `replay` is a determinism property, and `recommend` is not implemented (PROPOSED). The synthetic data is generated in memory from fixed seeds rather than read from files. The only file input is the operator's policy file: a closed schema, symlinks refused, size bounded, identity checked against TOCTOU.

Later designs must separately specify:

- strong identity and pairing where the platform can support it; ownership proof and revocation;
- permissions scoped by entity, capability, principal, target and expiry;
- request signing/authentication between distinct processes, replay protection and rate limits;
- explicit human confirmation for sensitive operations, with a second safety boundary outside any LLM;
- authorization on every action, not just at initial enrollment;
- supply-chain checks, reproducible builds, dependency licensing and configurable telemetry retention;
- prompt-injection containment: observations, web content, tool results and model text are untrusted;
- local-first and least-privilege data egress (no cloud request by default).

**Out of scope indefinitely unless a separate safety program is established:** unbounded actuator control; braking, steering, vehicle reprogramming, medical devices, high-voltage controls, industrial interlocks and spacecraft actuation.

## 5. MVP: falsify the "universal" architecture, not fake a platform

Build a **local, offline, deterministic synthetic proof of concept** with **three heterogeneous examples sharing the same event envelope and assessment interface**:

| Domain | Source | Example observation | What it proves |
|---|---|---|---|
| Virtual appliance | Synthetic fridge temperature / compressor state file | irregular cooling cycles | The agent need not run on an appliance |
| Vehicle | Synthetic CAN replay file from the independently reviewed v0.4 lab | decoded DBC-backed signal | Existing vehicle research can be adapted without changing core behavior |

> **Reconciliation note.** The implemented vehicle adapter decodes in-memory synthetic frames through its own test signal table. It shares no code with the v0.4 lab (PR #10) and does not depend on cantools. Wrapping the lab's output remains PROPOSED.
| Digital service | Synthetic server uptime/latency/error-count file | latency degradation | The contract is not tied to physical sensors |

The PoC **does not** need three different machine-learning models. Start with one deterministic rule or descriptive summarizer per context. Model inference, cross-agent coordination, energy optimization and live hardware are separate research problems.

The shared read-only observations must also be exposed through **one optional local MCP server**. Run independent transport/client conformance tests against that server (MCP protocol and, if feasible, OpenAI and Anthropic SDK integrations) without deploying to a real appliance or using paid models. Record incompatibilities honestly. The protocol-facing adapter must not be imported by the existing v0.1-v0.3 core on ordinary runs.

### Measurable gates

1. **Offline / no effects:** all tests use local files; no sockets, CAN devices, subprocess, external commands or hardware writes.
2. **Isolation:** entity A's observations do not change entity B's output. Explicitly prove denied cross-entity access and revoked capabilities.
3. **Replay:** given the same inputs, fixed source version and configuration, outputs are byte-identical across runs (with optional reproducibility variation documented across Python versions).
4. **Honest evidence:** unknown, missing, delayed or untrusted inputs produce explicit quality and abstention states, not invented health claims.
5. **Bounds:** configurable byte, line, row, entity, per-entity state and result sizes; measure resource use on a constrained workstation.
6. **Safety:** simulated attempts to request `transmit`, `write`, `install`, `scan` or `enroll` without an explicit grant fail closed.
7. **Compatibility:** historical v0.1 comparator and v0.2 benchmark bytes unchanged, v0.3 Scania preflight isolated, and v0.4 CAN experiment unaffected.
8. **Independent review:** open PR, offline CI and adversarial tests. No merge before review; no deployment to real devices.

### Expected artifacts (not code in this ADR PR)

- `AgentManifest` / `Entity` / `Capability` / `Observation` / `Assessment` draft schemas;
- local replay API; one small test fixture per domain; quality/evidence semantics and a compact privacy-safe manifest;
- compatibility and security tests; measured resource profile;
- a follow-up threat model before any real-world device API is added.

## 6. Migration, not a rewrite

- **v0.1** `core`: frozen historical reference, do not change its decision rule.
- **v0.2** `ingest`, `quality`, `reference`, `detect`, `benchmark`: retain old CSV schema, output, and deterministic quick benchmark.
- **v0.3** `componentx`: keep independent of agent enrollment and any ML stack; real Scania files/labels are **not** validated yet.
- **v0.4 Lab A** `cantools`: follow issue #7, strict read-only offline file replay. A later reviewed adapter may **wrap** its output, not force changes to the experiment.
- **Source provenance PR #6:** remains independent, with its own review/merge criteria.

Only build a separate `pexislink_agents` module/package after the event and policy contracts pass the synthetic three-domain PoC.

> **Reconciliation note (IMPLEMENTED).** The PoC is the subpackage `pexislink.fabric`, inside the existing distribution, with the MCP transport as one optional module (decision D1). Architecture tests enforce the isolation in both directions. A separate distribution can follow later without API changes. Do not split repositories or implement a service mesh without measurements demonstrating a real need. Product diagrams are not implementation evidence.

## 7. Alternatives considered

- **Automotive-only product:** simple and testable, but excludes the owner's intended horizontal scope. Retain automotive as a concrete vertical.
- **One universal LLM everywhere:** unsupported by low-end devices, costly and difficult to validate; inappropriate for safety and deterministic preflight.
- **Cloud-only central agent:** easier to synchronize but violates offline-first, privacy and resource goals; allow opt-in remote execution later.
- **Self-discovering / automatically installing agent swarm:** rejected for consent, security and operational safety reasons.
- **Rewrite current detector now:** rejected because it would destroy scientific baselines and blur immature interfaces.
- **Full IoT platform from day one:** rejected as overengineering; reuse permitted protocols and existing open implementations where justified.
- **Consumer-first PexisLink Mobile application:** rejected because we are building reusable infrastructure for independent AI providers and developers, not an app brand or competing assistant.
- **New universal agent communication protocol instead of MCP:** rejected because interoperability with existing MCP hosts is the objective; extend the domain semantics behind established protocols.

## 8. Open questions for the first review

1. Should the long-term product expose one owner-scoped identity per entity, or distinguish both a device identity and a separate digital-twin identity?
2. How will offline capability revocations propagate once networking is introduced, and what expires safely when disconnected?
3. Which *first* authorized real integration provides meaningful measurements without complex hardware or privileged APIs?
4. How do we version typed observation schemas without leaking vendor-specific models into the universal core?
5. What is the latency/memory ceiling for a gateway on low-end hardware, and when does a separate agent process become justified?
6. What precisely does the PexisLink runtime add beyond a basic MCP server, and can an independent developer verify the distinction in minutes?
7. Which OpenAI and Anthropic client/SDK combinations must form the versioned compatibility gate without requiring the companies' direct endorsement?

### Partial answers from the implemented slice (2026-10-16)

- **Q6.** What a basic MCP server lacks and the runtime adds:
  - deny-by-default grants bound to an operator-assigned principal;
  - evidence states derived by the core;
  - freshness, stale and abstention semantics;
  - typed, bounded, closed observation contracts;
  - an audit log with explicit events.

  A developer can verify these in minutes with `pexislink fabric demo` and the MCP examples (AGENT-FABRIC.md §1, §3).
- **Q7.** One gate exists today: the published OpenAI Agents SDK over stdio, with 15/15 cases on Python 3.11–3.13. Claude Code, Codex CLI and the Claude Agent SDK are PROPOSED gates.
- **Q1–Q5** remain open.

**Decision gate:** Approve or revise this direction. The first read-only slice has been authorized and merged (PRs #11 and #12). Every item in §0 *PROPOSED* and *BLOCKED* still needs explicit owner approval before code. Issue #8 is the sole architecture tracker for PexisLink Everywhere.
