# Multi-provider compatibility lab v0.1

**Status:** test-only lab. No PexisLink runtime code was changed for any client. No client is a
dependency of PexisLink. No model was called and no credential was used by any suite.
This is not an endorsement, certification or official integration by OpenAI, Anthropic or anyone
else, and it is not production validation.

This lab measures, through each client's **public, documented interface**, whether real agent clients
can use the PexisLink MCP stdio server (`pexislink fabric mcp-stdio`) correctly and safely. It extends
the OpenAI Agents SDK lab ([COMPAT-OPENAI-AGENTS.md](COMPAT-OPENAI-AGENTS.md)). Those 15 cases are
unchanged.

## Three different facts, never conflated

| Fact | Meaning | How it is proven |
|---|---|---|
| **Configured** | The client stored the server definition | The client's own config command reads it back (`codex mcp list --json`, `claude mcp get`). C02 also proves that listing the config does **not** start the server |
| **Connected** | The client launched the process and completed the MCP handshake | Client status (`mcpServerStatus/list` serverInfo, `✔ Connected`), plus a server-side env dump proving the process ran. A broken server must show up as failed (C02, K03) |
| **Executed** | A tool call ran in the PexisLink core and its result came back through the client | `structuredContent` produced by `Session.call`, relayed by the client, checked against the expected core result. **Discovering a tool does not prove it was called** |

## Status taxonomy

`PASS`: every test that ran passed. `FAIL`: a test failed or errored. `NOT_RUN`: it would need a
client, credential or model call that is not available. `NOT_SUPPORTED`: the client's public
interface cannot express the check. A skip without a `not_run:` or `not_supported:` reason counts as
`FAIL`, so a silent skip can never be reported as passing (`tests/compat/run_clients.py`).

## Clients

| Client | Version | License | Interface used | Runs in CI | Result |
|---|---|---|---|---|---|
| OpenAI Agents SDK | 0.22.3 (hash-locked) | MIT | `agents.mcp.MCPServerStdio` | yes (3.11–3.13) | **15/15 PASS** (unchanged) |
| Codex CLI | 0.160.0 (npm, integrity-locked; released 2026-10-01, ≥ 14 days) | Apache-2.0 | `codex app-server` JSON-RPC (`initialize`, `mcpServerStatus/list`, `thread/start` ephemeral, `mcpServer/tool/call`) and `codex mcp add/list` | yes (3.12) | **13/13 PASS**; one sub-check NOT_SUPPORTED (see below) |
| Claude Code | 2.1.301 (pre-installed in this environment) | proprietary | `claude mcp add/get/list` only | **no**, local only | **6 PASS, 2 NOT_RUN** |
| Claude Agent SDK | — | — | — | no | **NOT_RUN** (every entry point starts a model turn) |

Reports: `reports/compat/multi-client-*.json` (schema `pexislink.compatibility.multi_client.v0`).

### Case matrix

| Directive case | OpenAI Agents SDK | Codex CLI | Claude Code | Claude Agent SDK |
|---|---|---|---|---|
| Initialization and negotiation | PASS (T01) | PASS (C01)¹ | PASS (K02, health check) | NOT_RUN |
| Configured ≠ connected ≠ executed | PASS (T03) | PASS (C02) | PASS (K01–K04)² | NOT_RUN |
| Discovery of the 5 tools | PASS (T02) | PASS (C03) | NOT_RUN (K07)³ | NOT_RUN |
| Authorized query | PASS (T03) | PASS (C04) | NOT_RUN (K08)³ | NOT_RUN |
| Denied access | PASS (T04) | PASS (C05) | NOT_RUN (K08)³ | NOT_RUN |
| Forged identity | PASS (T05) | PASS (C06) | NOT_RUN (K08)³ | NOT_RUN |
| Expired and revoked permission | PASS (T06) | PASS (C07) | NOT_RUN (K08)³ | NOT_RUN |
| Session isolation | PASS (T11) | PASS (C08) | NOT_RUN (K08)³ | NOT_RUN |
| Schemas and pagination | PASS (T07) | PASS (C09) | NOT_RUN (K08)³ | NOT_RUN |
| Error sanitization | PASS (T10) | PASS (C10) | NOT_RUN (K08)³ | NOT_RUN |
| Determinism | PASS (T12) | PASS (C11) | NOT_RUN (K08)³ | NOT_RUN |
| No write tools, no effects | PASS (T13) | PASS (C12) | NOT_RUN (K08)³ | NOT_RUN |
| Process shutdown | PASS (T14) | PASS (C13) | PASS (K06) | NOT_RUN |

¹ The negotiated MCP protocol version is not exposed by any public app-server request: that
sub-check is `NOT_SUPPORTED`. serverInfo, capabilities and the client user agent are checked.
² Claude Code proves configured and connected only. It also proves that an **unapproved** project
`.mcp.json` server is not launched (K04).
³ Claude Code lists and calls MCP tools only inside a model turn, which needs authentication and
billable model calls. These cases are **not** claimed for Claude Code. The same server behaviour is
proven through Codex and the OpenAI Agents SDK, which is a different claim.

## Containment

| Client | Barriers |
|---|---|
| Codex CLI | (1) The model provider is an unroutable local URL, and every HTTP(S) proxy variable points at `127.0.0.1:9`. (2) The startup plugin, app, sync and update features are disabled in an isolated `CODEX_HOME`. (3) Detection: `RUST_LOG=info`, and the suite asserts that no outbound-request line appears. A negative check (features re-enabled) produced 4 such lines, so the detector is not vacuous. Codex receives an explicit environment, a fresh `HOME`/`CODEX_HOME`, and no login. Only model-free requests are sent; no turn is ever started. Server-to-client requests (approvals, elicitation) are refused |
| Claude Code | An explicit environment, a fresh `HOME` and `CLAUDE_CONFIG_DIR`, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY`, `DISABLE_AUTOUPDATER`, with unroutable proxies. The test helper **refuses** any argument vector other than `--version` and `mcp add/get/list`, because any other invocation could start a model turn |
| Server side (all clients) | The fixture's CPython audit log may contain only the measured AF_UNIX socketpair and a read-only `/dev/null` open (C12, K02, T13) |

## Findings

| # | Client | Finding | Consequence |
|---|---|---|---|
| I7 | Claude Code 2.1.301 | Stdio MCP servers receive the **client's whole environment**: a canary set for the client reached PexisLink (K05, pinned) | PexisLink never returns environment values. Still, operators must not run the client with secrets they would not hand the server. Codex, in contrast, passes only the configured `env` plus `HOME` and `PATH` (C10) |
| I8 | Codex CLI 0.160.0 | Without credentials, `codex app-server` still attempts outbound requests at startup (featured plugins on chatgpt.com, a curated plugin sync via `git ls-remote` and api.github.com) unless `plugins`, `remote_plugin` and `apps` are disabled | Contained in the suite as described above. Operators who want an offline client must disable these features |
| I9 | Codex CLI 0.160.0 | The negotiated MCP protocol version is not exposed | C01 sub-check `NOT_SUPPORTED` |
| I10 | Claude Code 2.1.301 | `claude mcp list` and `claude mcp get` launch every configured server for a health check (each took ~19 s here with egress blocked) | Expected and documented; listing has side effects (process launch), unlike `codex mcp list` |
| — | Codex CLI | Identical tool names on two servers are routed by server name; no collision (C08) | Contrast with I4 for the OpenAI Agents SDK |

I1 (strict schema conversion) and I2 (null for optional arguments) are client-independent contract
questions. C09 confirms I2 through Codex. The proposed resolution is in
[CONTRACTS-v0.2-PROPOSAL.md](CONTRACTS-v0.2-PROPOSAL.md).

## Evidence of test strength

- **Mutation:** with `PolicyEngine.decide` mutated to grant without a grant and to ignore
  revocation, the Codex suite fails 4 tests (C05, C06, C07 revocation, C08).
- **Negative egress check:** with Codex's network features re-enabled, the egress detector reports 4
  lines.
- **Incident disclosure:** during exploration, before these suites existed, one malformed invocation
  (`claude --debug mcp list`, where `--debug` consumed `mcp` as its filter) made Claude Code start a
  model turn with the prompt "list". No tool was called. That is why the helper now refuses every
  argument vector outside the allowlist. No suite makes a model call.

## Reproduce

```sh
# Server SDK and the OpenAI Agents SDK (hash-locked)
python -m pip install --require-hashes -r requirements/compat-openai-agents.txt
# Codex CLI (integrity-locked, no install scripts)
npm ci --ignore-scripts --prefix requirements/compat-codex
export PEXISLINK_CODEX_BIN=$PWD/requirements/compat-codex/node_modules/.bin/codex
# Optional, local only: an existing Claude Code installation
export PEXISLINK_CLAUDE_BIN=$(command -v claude)
PYTHONPATH=src python tests/compat/run_clients.py --json multi-client.json --require openai-agents codex-cli
```

## Not proven

- Tool calls through Claude Code and anything through the Claude Agent SDK (NOT_RUN, see above).
- Codex client-side effects are checked through its log, not through an audit hook (native code).
- `codex app-server` is marked experimental by Codex; its protocol may change. The pinned version
  keeps CI reproducible.
- Only Linux x86-64 was run. macOS and Windows were not tested.
- Model behaviour (whether a model picks the right tool or respects error text) is outside this lab.
