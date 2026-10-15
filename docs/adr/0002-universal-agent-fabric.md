# ADR 0002 (Proposed): Provider-neutral PexisLink Agent/Device Infrastructure

- **Status:** PROPOSED, pending owner and engineering review
- **Date:** 2026-10-15
- **Tracking issue:** [#8](https://github.com/guilhermehrcst/Pexislink/issues/8)
- **Scope:** long-term product architecture only. This ADR changes **no** code, permissions, deployment or existing release behavior.

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

\`\`\`text
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
\`\`\`

**First minimal read-only MCP tool candidates** (names are provisional; freeze only after conformance review):

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

Only build a separate `pexislink_agents` module/package after the event and policy contracts pass the synthetic three-domain PoC. Do not split repositories or implement a service mesh without measurements demonstrating a real need. Product diagrams are not implementation evidence.

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

**Decision gate:** Approve or revise this direction before authorizing code. Issue #8 is the sole architecture tracker for PexisLink Everywhere.
