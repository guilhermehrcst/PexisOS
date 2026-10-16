# Interoperability: OpenAI Agents SDK as an MCP client of the Agent Fabric

Other clients (Codex CLI, Claude Code, Claude Agent SDK): see [COMPAT-MULTI-PROVIDER.md](COMPAT-MULTI-PROVIDER.md).

> [!IMPORTANT]
> This is an **optional, test-only** compatibility suite. It shows that an independent, published SDK can consume the PexisLink MCP tool contract over local stdio:
> - **without** a model, an API key, Codex, a cloud service or a device;
> - with authorization still decided only by the PexisLink server.
>
> It is not an endorsement, certification or official integration by OpenAI, Anthropic or anyone else, and it is not production validation. The PexisLink runtime does not depend on, or import, the SDK.

## 5-minute quick start (no API key)

Python 3.11, 3.12 or 3.13. Use a fresh virtual environment, because the SDK pulls in client libraries that PexisLink itself never needs.

~~~bash
python -m venv .venv-compat && . .venv-compat/bin/activate
pip install --require-hashes -r requirements/compat-openai-agents.txt   # openai-agents 0.22.3, mcp 2.2.0
PYTHONPATH=src python tests/compat/run_matrix.py --json compat-report.json
~~~

Expected result (abridged): 15 PASS, 0 FAIL, 0 NOT_RUN.

```json
"summary": {"FAIL": 0, "NOT_RUN": 0, "PASS": 15}
"T01": {"name": "initialize", "status": "PASS", ...}  ...  "T15": {"name": "no_runtime_dependency", "status": "PASS", ...}
```

How the suite runs:
- The SDK's `agents.mcp.MCPServerStdio` launches `python -m pexislink fabric mcp-stdio --principal <id>` as a child process.
- A few cases launch the test-only `tests/compat/_fixture_server.py` instead.
- The SDK then performs the MCP handshake and calls the tools directly.

No `Runner`, `Agent` run, model or network is involved. Without the SDK installed, the same command reports 14 NOT_RUN plus T15 PASS, because T15 checks the *absence* of the dependency.

Minimal client code:

```python
from agents.mcp import MCPServerStdio
params = {"command": sys.executable,
          "args": ["-m", "pexislink", "fabric", "mcp-stdio", "--principal", "demo:principal:guest"],
          "env": {"PYTHONPATH": "src", "PATH": os.environ["PATH"]}}        # explicit, no credentials
async with MCPServerStdio(params=params, name="pexislink") as server:
    tools = await server.list_tools()                                       # 5 read-only tools
    result = await server.call_tool("list_entities", {})                    # result.structured_content
```

## Versions and provenance

| Component | Version | Source | Notes |
|---|---|---|---|
| openai-agents (locked, CI) | **0.22.3** (released 2026-09-17) | PyPI, hash-locked in `requirements/compat-openai-agents.txt` | The newest release at least 14 days old when locked. This is the repository's supply-chain rule (decision D9). |
| openai-agents (local run only) | **0.24.0** (released 2026-10-15) | PyPI, separate venv, not hash-locked | Executed once to check the version named in the compatibility kit. It is too new for the lock under D9. Bump it after 2026-10-29. |
| mcp / mcp-types | 2.2.0 | the same pin as `requirements/fabric-mcp.txt` | Used by both the server and the SDK client in one environment |
| Negotiated MCP protocol | `2026-07-28` | read from the live session | Client and server both run mcp 2.2.0 |
| PexisLink server | Agent Fabric 0.1.0, included in `main` via PR #11 merge `f7c6a7a` | this repository | No server code was changed for this suite |

The compatibility kit is a curated bundle of upstream files, with byte-for-byte hashes, from `openai/codex` (Apache-2.0) and `openai/openai-agents-python` (MIT).
- It was verified with its own `verify_bundle.py` (26 files PASS) and **read only**.
- Nothing from it is vendored, copied or executed in this repository. The integration uses the published package from PyPI.
- No NOTICE obligation is therefore triggered. Upstream licences remain with the upstream projects.

## Results (TEST_MATRIX.json)

Local runs on Linux x86_64 with CPython. Command: `PYTHONPATH=src python tests/compat/run_matrix.py`. The JSON reports are in `reports/compat/`.

| Environment | openai-agents | mcp | PASS | FAIL | NOT_RUN |
|---|---|---|---|---|---|
| Python 3.11.17 | 0.22.3 (lock) | 2.2.0 | 15 | 0 | 0 |
| Python 3.12.3 | 0.22.3 (lock) | 2.2.0 | 15 | 0 | 0 |
| Python 3.13.16 | 0.22.3 (lock) | 2.2.0 | 15 | 0 | 0 |
| Python 3.12.3 | 0.24.0 (unlocked) | 2.2.0 | 15 | 0 | 0 |
| Python 3.11.17 | not installed | not installed | 1 (T15) | 0 | 14 |

| Id | Case | What the real-SDK tests verify |
|---|---|---|
| T01 | initialize | The handshake completes. The negotiated protocol is one the SDK supports, and `server_info` = `pexislink-agent-fabric` / `0.1.0`. |
| T02 | discover | Exactly 5 tools, all `readOnlyHint=true`, `destructiveHint=false`, `openWorldHint=false`, with closed input schemas and no write-like names. Name collisions across two servers: the SDK refuses the duplicate set unless `include_server_in_tool_names`, in which case 10 distinct prefixed names. |
| T03 | observe_authorized | The analyst reads the latest fridge observation. `structuredContent` equals the JSON text, the schema version is `pexislink.observation.v0`, and the data is `current`. |
| T04 | hide_unauthorized | The guest sees only the fridge. A granted-elsewhere entity and an absent entity give byte-identical `not_found`. |
| T05 | reject_spoofed_principal | `principal` / `as_principal` arguments are rejected (`invalid_arguments`). This holds on the direct path and on the SDK's model-facing invocation path (`MCPUtil.invoke_mcp_tool` with model-style JSON). Identity is unchanged afterwards. |
| T06 | expired_revoked | A policy-file grant expired before the server clock is refused (`access_denied/expired`). Revocation takes effect mid-session through the SDK (`access_denied/revoked`, using the fixture's operator-side revoke). |
| T07 | schemas_and_cursor | The published schemas equal `tool_specs()` exactly. A real `next_cursor` round trip works. Invalid cursors and `mode=latest` with `limit` are rejected. Interoperability issues I1 and I2 are pinned by tests. |
| T08 | freshness_and_quality | The stale service shows `stale=true, current=false`, and its assessment is `stale` with `kind: inference`. Across the full vehicle history (paged), 17 `unknown` records have an empty payload or an `out_of_range` flag. |
| T09 | large_and_nonfinite | A 50-item page is ≤ 256 KiB. An over-budget result (fixture `--max-result-bytes 200`) returns `internal_error` with no structured content. All assessment JSON parses with NaN and Infinity rejected. The non-finite abstention path is verified in the core layer (`tests/test_fabric_final_review.py`), because the contract prevents it over the demo. |
| T10 | error_sanitization | Errors for hostile arguments, an unknown tool and an unknown signal never contain the markers, paths, `Traceback`, the source path or the canary value. The server process environment is asserted **exactly**: the client's explicit `PYTHONPATH`, `PYTHONNOUSERSITE` and `PATH`, plus the MCP stdio transport's own inheritance allowlist as present on the host (`mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS`: here `HOME`, `SHELL`, `TERM`), plus `LC_CTYPE` when the child interpreter performs PEP 538 locale coercion. Nothing else, and no host canary. |
| T11 | isolation | Two concurrent SDK sessions (guest and analyst, separate processes): the guest's view is unchanged by the analyst's parallel reads, and every item belongs to the requested entity. |
| T12 | replay_determinism | Two fresh server processes give byte-identical results for 5 calls. |
| T13 | negative_hardware | CPython audit hooks in **both** the server and the client process record every event in these families: all `socket.*`; process creation and control (`subprocess`, `exec`, `spawn`, `fork`, `kill`, `killpg`); `ctypes`; and opens whose *normalized* path (bytes decoded, `..` resolved) is under `/dev`, `/sys` or `/proc`. The hooks run while a session calls every tool. Only these measured events are allowed: <ul><li>an AF_UNIX `socket.__new__` in both processes (asyncio's self-pipe);</li><li>`/dev/null` opened **read-only** in the server (I6);</li><li>exactly one `subprocess.Popen` in the client, whose argv is the configured server.</li></ul> Write, transmit, execute, scan and OBD tools do not exist (`unknown_tool`). |
| T14 | lifecycle | Three connect, call, cleanup cycles on one `MCPServerStdio`: identical results, no leftover child processes, no file-descriptor growth in the client. |
| T15 | no_runtime_dependency | The core imports, and runs the demo, in a clean interpreter without loading `agents`, `openai`, `codex`, `mcp` or `anthropic`. No file in `src/` imports a provider SDK (AST check). |

Mutation checks, run during development and not committed:
- **Transport bypass.** A transport that bypasses `Session.call` made 5 tests fail: T05, T06, T07, T09 and T13.
- **Network access.** A server that opens a TCP connection during `tools/list` made T13 fail, with the log `socket.getaddrinfo`, `socket.connect`.
- **Review of `1bb9468`.** After the fix, T13 fails on each of:
  - `socket.gethostbyname` in the server;
  - `os.kill` in the server;
  - `open(b"/tmp/../dev/urandom")` in the server;
  - a client-side `getaddrinfo`.

  The previous watcher missed the first three, and it never audited the client.

## Interoperability findings

| # | Finding | Impact | Recommendation |
|---|---|---|---|
| I1 | **Strict schema conversion breaks `read_observations`.** With `convert_schemas_to_strict=True` (opt-in; the default is `False`), the SDK marks every property required without making optional ones nullable, and keeps `if`/`then`. The strict schema then requires `cursor` and `limit`, which `mode=latest` forbids, so it cannot be satisfied for `latest`. | An Agent configured for strict tool schemas cannot express a valid `latest` call. | Keep the default (non-strict) conversion, which passes the PexisLink schema through unchanged (verified). A future PexisLink contract may split `read_observations` into `read_latest` and `read_history`. That is a contract change for separate review, not made here. |
| I2 | **`null` is not "absent".** A model in strict mode may send `null` for optional fields. The core returns `invalid_arguments`, which matches its published schema. | Retries are needed from such clients. | Decide explicitly in a later contract version whether `null` means "absent". The current behaviour is consistent and tested. |
| I3 | **`server_initialize_result` is `None`.** With mcp 2.2.0 and protocol `2026-07-28`, the SDK's `MCPServerStdio.server_initialize_result` is `None`. | Version information is still available through `server.session.server_info` and `server.session.protocol_version`. | T01 uses the session accessors. |
| I4 | **Tool names are not namespaced by default.** Two PexisLink servers in one agent collide unless `include_server_in_tool_names=True`. | The SDK refuses the duplicate set rather than routing ambiguously, which is the safe behaviour. | Enable prefixing when connecting more than one PexisLink server. |
| I5 | **Errors reach the model as text only.** On the SDK's model-facing path (`invoke_mcp_tool`), PexisLink error results arrive as text, and the `isError` flag is not visible in that output. | The sanitized error JSON is what a model sees. | No action: authorization is server-side and errors carry no sensitive data. |
| I6 | **`/dev/null` opened by the MCP transport.** The official MCP stdio transport opens `/dev/null` read-only to divert fd 0. This is outside the PexisLink core and its `no_effects()` guard. | None: read-only, no data. | T13 allows exactly this event and nothing else. |

## Security boundaries

- **Authorization is server-side only** (`Session.call`: deny by default, per principal, per entity, per capability, with expiry and revocation). SDK tool filters, `require_approval` and tool guardrails are client-side conveniences and are never treated as authorization.
- **The principal comes from the launch configuration** (`--principal`), never from tool arguments or model output.
- **The child environment is known exactly.** The client passes `PYTHONPATH`, `PYTHONNOUSERSITE` and `PATH`. The MCP stdio transport adds its own inheritance allowlist (`HOME`, `LOGNAME`, `SHELL`, `TERM` and `USER` on POSIX, when set on the host), and the child interpreter may add `LC_CTYPE` (PEP 538). T10 asserts this exact set, so any other host variable, including credentials, is never forwarded.
- **Local stdio only.** No hosted MCP, no HTTP or SSE transport, no Responses-API hosted tools, and no outbound network in the tests.
- **The fixture server is test-only.** It exposes operator-side knobs (revoke after N calls, a smaller result limit, an audit log, an environment-name dump) that the production CLI deliberately lacks.
- **The Codex references were read for design practice only:** per-call authority and version-pinned conformance baselines. Nothing was adopted from them, specifically:
  - Codex execpolicy's implicit `allow` default (PexisLink stays deny-by-default);
  - the experimental sandbox metadata;
  - any shell or execution tool.

## Dependencies and licences (test-only)

The packages are installed only in the compatibility venv. They are not distributed with PexisLink and not imported by it.

| Licence | Packages |
|---|---|
| MIT | openai-agents 0.22.3, jiter, charset-normalizer, urllib3, plus the MIT packages already listed for `fabric-mcp` |
| Apache-2.0 | openai 3.24.0, requests |
| ISC | griffelib |
| BSD-3-Clause | websockets |
| MIT OR Apache-2.0 | sniffio |
| MPL-2.0 | certifi. This is weak, file-level copyleft. It is used unmodified as a test dependency and never distributed, so no obligation arises for PexisLink. |

All other pins are identical to `requirements/fabric-mcp.txt`, with the licences listed in `docs/AGENT-FABRIC.md`.

## Not run, not verified and open review gates

| Item | Status | Reason |
|---|---|---|
| An LLM-driven `Runner.run` using these tools | NOT_RUN | Requires a model and an API key, which is out of scope by design |
| Hosted MCP / connectors (Responses API, remote HTTP) | NOT_RUN | Requires a network-reachable authenticated server. Blocked by Agent Fabric decision B1. |
| Whether a hosted model API accepts the PexisLink schemas in strict mode (`pattern`, `if`/`then`) | NOT_VERIFIED | Needs the hosted API. I1 already shows the SDK's strict conversion is unsatisfiable for `latest`. |
| Codex CLI as a client | Covered elsewhere | Not part of this suite. A separate CI job, `compat-codex-cli`, installs a pinned, integrity-locked, test-only Codex CLI and requires every Codex case to PASS (`run_clients.py --require codex-cli`), with no login, no credential and no model call. See [COMPAT-MULTI-PROVIDER.md](COMPAT-MULTI-PROVIDER.md) |
| openai-agents 0.23.1 | NOT_RUN | Only 0.22.3 (lock) and 0.24.0 (latest) were executed |
| Windows / macOS | NOT_RUN | Linux x86_64 only |
| Independent security review of this suite | PENDING | Requested on the PR |
