# ADR 0002 (Proposed): PexisLink Everywhere / Universal Agent Fabric

- **Status:** PROPOSED, pending owner and engineering review
- **Date:** 2026-10-15
- **Tracking issue:** [#8](https://github.com/guilhermehrcst/Pexislink/issues/8)
- **Scope:** long-term product architecture only. This ADR changes **no** code, permissions, deployment or existing release behavior.

## 1. Why this exists

PexisLink started as **offline vehicle telemetry and anomaly research**. The proposed long-term ambition is larger: a **permissioned agent fabric** in which heterogeneous *physical and digital entities* can be represented by agents that observe authorized signals, keep bounded context and assist their owners. This includes vehicles, appliances, robots, factory equipment, satellites (future separately qualified research), computers, mobile applications, servers, business services and software workflows.

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

### Mobile-first control plane: one phone, many agent locations

**Product requirement:** the end user should be able to observe, organize and, where explicitly permitted, manage their enrolled physical and digital entities from **one mobile interface**. Tentative experience name: **PexisLink Mobile**. This is the *control plane and user experience*, not the universal place where inference, collection, or all agents must run.

- **Phone:** single owner-scoped dashboard for devices/services grouped by place and type; time of last verified observation; agent status and permissions; explainable alerts; history; and an optional natural-language assistant for querying **authorized** observations. The app must distinguish unavailable, stale and insufficient evidence rather than invent live state.
- **Agent runtime:** stays on a compatible host, owner-controlled gateway or authorized remote connector, with independent resource budgets. Basic permitted monitoring should continue when the phone is offline, closed or powered down; the phone is not a single point of failure.
- **Connectivity:** same-LAN/local connections can function without cloud where the relevant local agents/gateway are reachable. Control from outside that LAN requires a **separate authorized connection** (e.g. an authenticated relay or user-provisioned VPN). A phone cannot reach arbitrary offline or non-networked equipment by magic.
- **Authorization:** explicit enrollment/pairing, owner verification as feasible, per-entity principals and shared-household/team roles, least-privilege and revocation, short-lived scoped credentials, safe handling of reconnects. A mobile biometric gate may protect sensitive *UI actions* but is not a replacement for server/agent-side authorization.
- **Action safety:** Stage 0 is **read-only**. Future low-risk operations (for example requesting a status refresh from an authorized API) require a separately reviewed capability policy. High-impact actions require confirmed intent and an independent policy check; an LLM cannot grant itself permissions. Vehicle actuators, safety interlocks and spacecraft control are out of scope.
- **Privacy and usability:** minimize data leaving the owner's control; offer configurable retention/export/delete controls, explicit offline and synchronization state, accessible mobile-first interactions, and transparency on which device/connector produced each observation.

**Proposed navigation:** Overview, Enrolled Entities, Alerts/Activity, Assistant, Permissions/Settings. This is a product direction, **not a shipped app or implemented API**.

#### Mobile-facing proof of concept (after the offline agent contracts)

Create a lightweight **mobile-responsive, read-only** viewer for **the same three synthetic domain fixtures** (virtual appliance, CAN replay, software service) from the core PoC. It may start as an installable PWA if platform constraints allow; evaluate a native iOS/Android application only when required capabilities such as local pairing or background integration justify native APIs. Neither a PWA nor a native app may circumvent iOS/Android background execution, platform permissions or network constraints.

Acceptance examples:

1. One screen shows all three synthetic entities using the same versioned event contract, per-entity provenance and **last observed** time; no cross-entity data mixing.
2. Offline browsing of previously authorized cached data clearly displays **stale/offline** status. It must not claim to show real-time data when disconnected.
3. An unenrolled entity, expired token or revoked permission is denied even if a user asks the natural-language assistant to override policy.
4. Closing the app does not stop an independently running local replay/agent, and reopening does not silently backfill missing observations as verified.
5. The mobile frontend does **not** add device discovery, remote shell, background scanning, CAN transmission, MQTT publish or arbitrary command execution in the MVP.



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

## 8. Open questions for the first review

1. Should the long-term product expose one owner-scoped identity per entity, or distinguish both a device identity and a separate digital-twin identity?
2. How will offline capability revocations propagate once networking is introduced, and what expires safely when disconnected?
3. Which *first* authorized real integration provides meaningful measurements without complex hardware or privileged APIs?
4. How do we version typed observation schemas without leaking vendor-specific models into the universal core?
5. What is the latency/memory ceiling for a gateway on low-end hardware, and when does a separate agent process become justified?

**Decision gate:** Approve or revise this direction before authorizing code. Issue #8 is the sole architecture tracker for PexisLink Everywhere.
