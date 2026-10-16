# PexisLink Agent Fabric v0.1 (experimental)

> [!IMPORTANT]
> **Status: experimental vertical slice. Interfaces are proposals, not a stable public API.**
> Everything runs offline on synthetic data. No physical device, bus, network
> service or paid API is touched. Assessments are inferences about a signal's
> recent behaviour. They are not diagnoses, fault findings, predictions or
> safety assessments.

The Agent Fabric is a small, read-only, policy-gated layer between AI agents and
observed entities. An entity can be physical or digital: a refrigerator, a
vehicle, a software service. Any client can consume the same structured,
evidence-qualified results: plain Python code, an independent MCP client, or an
agent built on any model provider.

- Threat model and the tests behind each control: [AGENT-FABRIC-THREAT-MODEL.md](AGENT-FABRIC-THREAT-MODEL.md)
- Preflight, decisions, and the items blocked pending approval: [AGENT-FABRIC-DECISIONS.md](AGENT-FABRIC-DECISIONS.md)
- Architectural direction this slice implements, still a proposal: ADR 0002, in draft PR #9

## 1. The problem, and how this differs from a plain MCP server

A plain MCP server exposes tools. It does not answer four questions an agent needs answered before it relies on data from the world:

| Question | Agent Fabric v0.1 answer |
|---|---|
| **May this caller see this entity, and for which purpose?** | Deny-by-default grants per principal, per entity and per capability. Grants expire and can be revoked. The principal is bound by trusted code when the session opens, never taken from tool arguments. |
| **How much can this value be trusted?** | Every observation carries an evidence state (`observed`, `late`, `unknown`) that the **core** derives. An adapter cannot claim `late`, and the core downgrades out-of-order, late and out-of-range data. Rejected input is counted and never stored. |
| **Is it current?** | Every `latest` read and every assessment states freshness against the entity's declared `stale_after_s`. Stale data is labelled `stale`, never shown as current. |
| **What is a measurement and what is an inference?** | Observations and assessments are separate documents. An assessment is marked `"kind": "inference"`, lists its method, parameters and numbers, and caps its confidence at `medium`. Too little evidence yields `insufficient`, never a guess. |

The same core API serves every transport. MCP is one optional adapter on top
of it; it neither duplicates nor bypasses the policy engine.

## 2. Architecture

```
            trusted operator code                              request side (agent)
  ┌──────────────────────────────────────┐        ┌───────────────────────────────────────┐
  │ adapters (domain knowledge lives here)│        │ Session (one principal, fixed at open) │
  │  appliance · vehicle(CAN) · service  │        │  call(tool, args) → dict | FabricError │
  └──────────────┬───────────────────────┘        └──────────────┬────────────────────────┘
                 │ raw observation dicts (untrusted)             │ 1 strict args (closed schema)
                 ▼                                               │ 2 policy decision (deny default)
  ┌──────────────────────────────────────┐                       │ 3 runs inside no_effects()
  │ Fabric.ingest → contracts + store     │◄──────────────────────┤ 4 bounded result
  │  validate · derive quality · bound   │   reads              │ 5 audit: decision + executed
  └──────────────────────────────────────┘                       │
  ┌──────────────────────────────────────┐                       ▼
  │ PolicyEngine (grants, revocation,     │        ┌───────────────────────────────────────┐
  │ injectable clock) · AuditLog (chain)  │        │ optional transport: mcp_server (stdio) │
  └──────────────────────────────────────┘        └───────────────────────────────────────┘
```

| Module (`src/pexislink/fabric/`) | Responsibility | Dependencies |
|---|---|---|
| `contracts.py` | Versioned closed schemas: `EntitySpec`, `SignalSpec`, observation contract, `Capability`, `EvidenceState`, timestamps, canonical JSON | stdlib |
| `policy.py` | `Principal`, `Grant`, `PolicyEngine`, `AuditLog`, operator policy-file loader | stdlib |
| `store.py` | Bounded per-entity in-process memory; ingest-time quality derivation and rejection | stdlib |
| `assess.py` | Domain-neutral robust assessment (median/MAD), explicit stale/insufficient | stdlib |
| `guard.py` | Audit-hook barrier: no sockets, processes, native loading, writes or device paths during request handling | stdlib |
| `runtime.py` | `Fabric` (operator side), `Session` (request side), tool schemas, sanitized errors | stdlib |
| `adapters/` | Synthetic refrigerator, synthetic CAN vehicle, synthetic software service | stdlib |
| `demo.py`, `cli.py` | Deterministic demo world; `pexislink fabric demo` / `mcp-stdio` | stdlib (+ `anyio` lazily for stdio) |
| `mcp_server.py` | Optional MCP transport over `Session` (official SDK low-level `Server`) | `mcp==2.2.0` (optional) |

Isolation is enforced by `tests/test_architecture.py`:

- the fabric imports nothing from the v0.1, v0.2 or v0.3 modules, the benchmark, or ground truth;
- no existing module imports the fabric (only the CLI dispatches to it);
- the core is standard library only;
- the MCP SDK is imported only by `mcp_server.py`, and importing the fabric does not load it;
- request-facing code never calls `add_grant` or `revoke`.

The core contains no domain vocabulary. A test fails if words such as
`fridge`, `vehicle`, `coolant` or `dbc` appear in a core module.

## 3. Install and run (no account, no API key, no device)

Python 3.11, 3.12 or 3.13.

~~~bash
python -m pip install -e .                      # core: zero runtime dependencies
pexislink fabric demo                           # deterministic walk-through, JSON on stdout
PYTHONPATH=src python examples/fabric/python_api.py

# Optional MCP transport (pinned, hash-checked):
python -m pip install --require-hashes -r requirements/fabric-mcp.txt   # or: pip install -e '.[mcp]'
PYTHONPATH=src python examples/fabric/mcp_client.py demo:principal:analyst
PYTHONPATH=src python examples/fabric/mcp_client.py demo:principal:guest
~~~

Expected `pexislink fabric demo` output, abridged. The output is byte-identical on every run and on Python 3.11, 3.12 and 3.13, SHA-256 `a7673188…cc0e15`:

```text
ingest              fridge-01: observed 222, late 7, unknown 1 · synthetic-01: observed 718, unknown 17 · checkout-api: observed 390
analyst.assessments fridge-01: deviation (temperature_c)   synthetic-01: deviation (coolant_temp_c)   checkout-api: stale
analyst.latest_service            {"age_s": 2430.0, "stale": true, "current": false, ...}
guest.vehicle_hidden              {"error": {"code": "not_found", ...}}      # identical to:
guest.nonexistent                 {"error": {"code": "not_found", ...}}
spoofed_principal_argument        {"error": {"code": "invalid_arguments", ...}}
write_tool_absent                 {"error": {"code": "unknown_tool", ...}}
after_revocation.analyst_fridge   {"error": {"code": "access_denied", "reason": "revoked", ...}}
audit                             {"records": 17, "executed": 7, "denied": 5, "chain_valid": true}
```

Expected `examples/fabric/mcp_client.py demo:principal:guest` output:

```text
tools: ['describe_entity', 'explain_assessment', 'list_capabilities', 'list_entities', 'read_observations']
entities: ['demo:appliance:fridge-01']
demo:appliance:fridge-01 -> {'code': 'access_denied', 'message': '...', 'reason': 'no_grant'}
spoofed principal argument -> invalid_arguments
```

## 4. Python API

```python
from pexislink.fabric import Fabric, Grant, ManualClock
fabric = Fabric(clock=ManualClock(now_us))          # or the default system clock
fabric.register(entity_spec)                         # operator: identity + declared capabilities
fabric.ingest(raw_observation)                       # -> IngestResult(accepted, code, seq, status)
fabric.policy.add_grant(Grant(grant_id, principal_id, entity_id, frozenset({...}), not_before_us, expires_at_us))
fabric.policy.revoke(grant_id)                       # immediate for every later decision
session = fabric.open_session(principal_id)          # trusted code binds the principal
session.tools()                                      # name -> {description, input_schema}
session.call("explain_assessment", {"entity_id": ...})   # dict, or raises FabricError(code)
```

Request-side tools are listed below. All are read-only, and every input schema is closed (`additionalProperties: false`).

| Tool | Required capability | Arguments |
|---|---|---|
| `list_entities` | `describe` (filters the list) | `cursor?`, `limit? (1–50)` |
| `describe_entity` | `describe` | `entity_id` |
| `list_capabilities` | `describe` | `entity_id` |
| `read_observations` | `observe` for `mode=latest`, `read_history` for `mode=history` | `entity_id`, `mode`, `cursor?`, `limit?` |
| `explain_assessment` | `assess` | `entity_id`, `signal?` |

Error codes are stable and messages fixed. Errors never include argument values, payloads, paths or stack traces:

- `invalid_arguments`
- `unknown_tool`
- `not_found`
- `access_denied`, with a `reason` from `no_grant`, `expired`, `revoked` or `not_yet_valid`
- `capability_not_supported`
- `internal_error`

**Existence is not disclosed.** An entity the principal holds no grant for at all answers `not_found`, byte-identical to an entity that does not exist.

`list_capabilities` keeps four notions apart:

- **declared**: the entity supports the capability;
- **available**: the entity has data to answer;
- **authorized**: this session may use the capability now;
- **executed**: the audit log has a record with `executed: true` for each completed call.

## 5. MCP integration

The server runs locally over **stdio** for exactly one principal:

~~~bash
pexislink fabric mcp-stdio --principal demo:principal:analyst            # built-in demo grants
pexislink fabric mcp-stdio --principal op:principal:fleet --policy policy.json
~~~

A typical MCP host configuration launches it as a child process. The operator who writes this configuration chooses the principal; the model cannot:

```json
{"command": "python", "args": ["-m", "pexislink", "fabric", "mcp-stdio", "--principal", "demo:principal:guest"],
 "env": {"PYTHONPATH": "/path/to/Pexislink/src"}}
```

Properties, each verified with the official SDK `Client` in `tests/test_fabric_mcp.py`:

- **Discovery.** There are 5 tools, each with a closed input schema and the annotations `readOnlyHint=true`, `destructiveHint=false`, `openWorldHint=false`.
- **Structured results.** Each result is in `structuredContent`, plus one JSON text block with the same content. The results are equal to what `Session.call` returns.
- **Policy.** Every call goes through `Session.call`. The transport contains no policy logic and cannot reach the store directly.
- **Errors.** Errors come back as `isError: true` results. Even a transport-level exception is reported as `internal_error`, with no text.
- **Tested transports:**
  - in-memory, in the SDK's `legacy` mode (full JSON-RPC framing) and in `auto` mode;
  - the real stdio transport against the CLI in a child process.

**Local SDK integration is not the same as a hosted connector.** Several agent frameworks and model APIs can call MCP servers, in some cases through hosted "connectors". Hosted connectors generally require a remote, network-reachable HTTP server with its own authentication. v0.1 deliberately ships **no network transport**: see decision D5 and the blocked item B1 in the decisions document. Compatibility with the MCP protocol is not adoption, certification or endorsement by any model provider.

## 6. Permission model

- **Principal.** An identifier (`namespace:kind:name`) bound when the session or server is created by trusted code: the operator's launch configuration. v0.1 does not authenticate principals cryptographically. The trust root is whoever can launch the process or call `open_session`.
- **Grant.** Has an exact principal, an exact entity (no wildcards), a non-empty set of read-only capabilities, `not_before`, and a **mandatory** `expires_at` (exclusive). A grant without expiry cannot be represented.
- **Decision order.** The first failing step decides, and each step fails closed:
  1. capability is read-only;
  2. entity exists;
  3. entity declares the capability;
  4. a matching grant exists;
  5. the grant is not revoked;
  6. the grant has started;
  7. the grant has not expired.

  Any exception yields `policy_error` (deny).
- **Revocation.** Takes effect at once, including inside a live MCP session. A revoked grant id can never be reused.
- **Audit.** A bounded, append-only, SHA-256 hash-chained log records each decision and each execution. It holds identifiers and reason codes only, never observation values.
- **Policy file.** `pexislink.policy.v0` is a closed schema of at most 64 KiB, with duplicate JSON keys and NaN rejected. The loader refuses symlinks (`O_NOFOLLOW`), checks that the file is regular, and verifies the file's identity before, during and after the read (TOCTOU). The identity check covers inode, size, mtime and ctime; ctime cannot be restored by a writer, so a same-size rewrite in place is detected.
- **Audit failure fails closed.** If the audit sink raises, the request returns `internal_error` and no unaudited result is served.

## 7. Data contracts (`v0`)

Observation (`pexislink.observation.v0`) has exactly these keys:

| Key | Contract |
|---|---|
| `schema_version` | `"pexislink.observation.v0"` |
| `event_id` | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`; unique per entity within retention |
| `entity_id`, `source` | `namespace:segment[:segment[:segment]]`, lowercase. `source` must equal the entity's adapter |
| `observed_at`, `received_at` | RFC 3339 UTC with `Z` only and at most 6 fractional digits (ASCII digits only). `received_at >= observed_at`, and not more than 5 s ahead of the clock |
| `kind` | dotted lowercase name |
| `payload` | at most 32 keys; each key is a declared signal name. Values: finite number, integer with \|i\| ≤ 2^53, boolean, or a declared enum value. Free text is rejected |
| `quality` | `{"status": "observed" or "unknown", "flags": [...]}`. Adapters may report `out_of_range`, `unknown_frame` or `length_mismatch`. `late_arrival` and `out_of_order` are derived by the core only |
| `provenance` | `{"source": "synthetic_fixture", "license": "project_original", "generator", "seed"?}` |

Evidence states:

| State | Meaning |
|---|---|
| `observed` | In order, on time, and every value inside its declared range |
| `late` | Valid values, but delayed beyond `max_latency_s` or out of order |
| `unknown` | Carries no trustworthy measurement: an unknown CAN frame, a length mismatch, or a value out of range. Never used by assessments |
| `rejected` | Failed the contract. Counted per reason code and never stored |
| `insufficient` | Not enough trustworthy values to conclude anything (assessment level) |

The entity document (`pexislink.entity.v0`) has these parts:

- identity, kind, display name and adapter;
- `declared_capabilities`;
- typed signals, with unit, plausible range, enum vocabulary and an `assessable` flag;
- timing: `expected_interval_s`, `max_latency_s` and `stale_after_s`.

The assessment document (`pexislink.assessment.v0`) contains:

- `kind: "inference"`, a notice, `assessed_at` and `state`;
- `confidence`: `low` or `medium`;
- `method`: the name and all parameters;
- inputs: counts by status, gaps, and first and last times;
- freshness;
- per-signal state, reason and evidence: baseline median and MAD, recent median, robust z and threshold.

### Versioning policy

- Every document carries an explicit schema identifier, and unknown versions are rejected.
- Within `v0`, fields are never removed or re-typed. Adding a field requires a new identifier, because schemas are closed and old readers reject unknown keys by design.
- Until the architecture in ADR 0002 is approved, `v0` may change between pre-releases. Each change is listed in the PR that makes it.

## 8. Assessment method (domain-neutral)

The method runs per declared numeric, assessable signal, over the last `window = 120` stored observations of the entity:

1. Keep only `observed` and `late` values, ordered by `observed_at`.
2. Freshness follows **usable** evidence:
   - if the latest `observed` or `late` observation is older than `stale_after_s`, the overall state is `stale`; a fresh `unknown` record never counts as fresh;
   - each signal is also `stale` on its own when its latest trustworthy value is older than `stale_after_s`.
3. With fewer than `min_points = 24` values, the signal is `insufficient`.
4. Otherwise compare the median of the last `recent = 6` values with the median and MAD of the earlier values. The scale is `max(1.4826·MAD, 0.001·declared range)`.
5. If `|z| > 4`, the signal is `deviation`; otherwise `nominal`.
   The overall state is the first match in the order `deviation`, `stale`, `nominal`, `insufficient`.
   With `signal=` the overall state and confidence are computed from that signal only.
6. Booleans, enums and signals declared non-assessable (counters) are `unknown`.

Confidence is `low` whenever data is stale, late, unknown or has gaps, and is never `high`.

Calibration evidence:

- **Refrigerator.** Seeds 1–59: robust z ≥ 5.66 after the synthetic weak-cooling period, and |z| ≤ 0.89 before it.
- **Vehicle.** `coolant_temp_c` rises at the end and is a `deviation`. A 655.35 V battery frame is `unknown` and does not move `battery_v`.

The data is written by this project, so these numbers check the implementation against its own assumptions. **They are not real-world detection performance.**

## 9. Limits

| Limit | Default | Where |
|---|---|---|
| Entities | 256 | `FabricLimits.max_entities` |
| Observations per entity (FIFO eviction, reported in `retention`) | 10 000 | `max_per_entity` |
| Page size | 1–50 | `max_page` |
| Serialized tool result | 256 KiB (pages shrink to fit) | `max_result_bytes` |
| Observation depth / payload keys / string length | 4 / 32 / 64 | `max_depth`, `max_keys`, `max_string` |
| Audit records | 10 000 (bounded suffix; chain verifiable) | `max_audit` |
| Grants | 1 024 | `PolicyEngine.max_grants` |
| Policy file | 64 KiB | `MAX_POLICY_BYTES` |
| Assessment window | 120 observations (≤ 1 000) | `AssessmentConfig` |

**Retention.** Data lives in process memory only, never on disk, and dies with the process. An event id evicted from the buffer can be ingested again; this is a documented limitation.

## 10. Measured resource use

These figures are machine-dependent measurements, not guarantees. Reproduce with `PYTHONPATH=src python scripts/fabric_measure.py`, adding `--memory` for the memory figures.

Environment: one entity with 10 000 observations; 4 vCPU Intel Xeon @ 2.10 GHz (x86_64), CPython. Medians over 50 calls:

| Metric | 3.11.17 | 3.12.3 | 3.13.16 |
|---|---|---|---|
| Ingest throughput (obs/s) | 21 748 | 25 661 | 22 980 |
| `read_observations` latest (ms) | 0.99 | 0.97 | 0.91 |
| `read_observations` history, page of 50 (ms) | 1.68 | 1.65 | 1.65 |
| `explain_assessment` (ms) | 0.26 | 0.26 | 0.25 |
| `list_entities` (ms) | 0.023 | 0.020 | 0.021 |
| Traced peak memory after ingest (MiB) | 8.68 | 8.61 | 8.68 |
| ≈ bytes per stored observation | 910 | 902 | 910 |

Result sizes: a 50-item history page is 24 975 bytes; an assessment is 1 411 bytes.

From these figures, a full store of 256 × 10 000 observations would hold about 2.3 GB. That is an **extrapolation, not a measurement**; lower `max_per_entity` or `max_entities` on small devices.

## 11. Dependencies and licences

**Core.** Python standard library only. PexisLink is Apache-2.0.

**Optional MCP transport.** `requirements/fabric-mcp.txt` is hash-locked. It was resolved with `--exclude-newer 2026-10-01`, so every pin was at least two weeks old when locked. Licences, read from the installed metadata, are all permissive:

| Licence | Packages |
|---|---|
| MIT | mcp 2.2.0 and mcp-types 2.2.0, annotated-types, anyio, attrs, h11, jsonschema, jsonschema-specifications, pydantic, pydantic-core, PyJWT, referencing, rpds-py, truststore, typing-inspection |
| MIT-0 | cffi |
| BSD-3-Clause | click, httpcore2, httpx2, idna, pycparser, sse-starlette, starlette, uvicorn |
| Apache-2.0 | opentelemetry-api, python-multipart |
| Apache-2.0 OR BSD-3-Clause | cryptography |
| PSF-2.0 | typing_extensions |

Two pins are platform-conditional and not installed on Linux. Their licences were read from PyPI metadata:

- `pywin32` (Windows only): PSF;
- `httpx2-jsfetch` (Emscripten only): BSD-3-Clause.

The SDK pulls in HTTP and server packages (starlette, uvicorn, httpx2) although v0.1 uses only stdio. That weight is the reason it is optional.

## 12. Limitations (honest scope)

- **Synthetic data only.** No real appliance, vehicle or service data has been ingested.
- **Identity.** Principals and entities are identifiers, not cryptographically proven identities. Synthetic ids are not evidence of any real asset.
- **Single process.** The server serves a single principal over local stdio only: no HTTP, no remote clients, no multi-tenant server.
- **Assessment method.** A deliberately simple level-shift detector. It does not model seasonality, duty cycles or cross-signal relations, and its confidence vocabulary is coarse.
- **Vehicle adapter.** Decodes only its own built-in test signal table (Intel byte order, classic CAN). It is independent of the v0.4 CAN replay lab (PR #10) and shares no code with it.
- **The guard is a tripwire, not a sandbox.** Audit hooks can be bypassed by native code, and the guard covers request handling only.
- **Only read-only capabilities exist.** Writing, transmitting, controlling, installing, scanning and enrolling cannot be represented in v0.1.
