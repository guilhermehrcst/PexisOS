# Agent Fabric v0.1: preflight, decisions and blocked items

## Marco 0: preflight (verified 2026-10-15)

| Item | Observed state |
|---|---|
| `main` | `2703c8d` (v0.3 Stage A preflight merged). Branch `feat/agent-fabric-v01` was created from it, not from any open PR |
| Open PRs | **#6**: Component X evidence update. **#9**: ADR 0002, draft, status PROPOSED. **#10**: v0.4 Lab A CAN replay; CI green; awaiting independent review; not merged |
| Issues | **#8**: provider-neutral MCP infrastructure vision. **#7**: CAN/DBC lab |
| Baseline suite on `main` | Green on Python 3.11, 3.12 and 3.13 (standard library only) |
| Quick benchmark | `results.json` regenerated; SHA-256 `33818dfd…547c7cb18`, identical to the committed report |

Overlap with the open PRs:

- **PR #10** touches `cli.py`, `tests/test_architecture.py`, `pyproject.toml`, `.github/workflows/tests.yml` and `README.md`, as this PR does. Each of those edits is additive (a new command, an extra, a job, a test class), so the conflicts are textual and mechanical to resolve. No code is shared. The vehicle adapter here does **not** use cantools, reads no file, and opens no CAN interface.
- **PR #9** is the architectural proposal. This PR implements a slice of it and treats every interface as a proposal (`v0` schemas).
- **PR #6** touches only Component X documentation. It does not overlap with this PR.

## Decisions taken in this slice

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | One subpackage, `pexislink.fabric`, inside the existing distribution, with the MCP transport as one optional module | Separate distributions (`pexislink_fabric`, `pexislink_mcp`) | It is the smallest structure that still enforces isolation. Architecture tests enforce the boundaries, and the default install stays dependency-free. Splitting distributions can come later without API changes |
| D2 | Use the official MCP Python SDK **v2** (`mcp==2.2.0`), low-level `Server` | v1 (FastMCP); the v2 high-level `MCPServer` | v2 is the current line, and v1's FastMCP is removed in v2. A prototype showed that `MCPServer` silently accepts extra arguments and echoes input values in validation errors. The low-level server lets the core own closed schemas and sanitized errors |
| D3 | Bind the principal at session construction, from the operator's launch configuration | A principal argument on each tool; tokens inside tool arguments | Tool arguments are model-controlled. The master prompt forbids trusting them for identity |
| D4 | In-process, bounded memory only | JSONL; sqlite3 | Persistence would add a filesystem surface (paths, permissions, retention on disk), and nothing in the slice needs it. Determinism and isolation are easier to prove in memory |
| D5 | **stdio transport only**, with no HTTP | Streamable HTTP | HTTP would need authentication, TLS and exposure decisions. It changes the security surface and is therefore **blocked pending approval** (B1) |
| D6 | Mandatory grant expiry; exact entity ids, no wildcards | Wildcards; optional expiry | Fail-safe defaults. Wildcards widen the blast radius of a mistaken grant |
| D7 | Free text never enters the fabric (enum vocabularies only) | Free strings with escaping | This removes the prompt-injection channel instead of trying to sanitize it |
| D8 | Unauthorized entities answer `not_found` | `access_denied` | Prevents enumerating which entities exist |
| D9 | Hash-locked optional requirements with `--exclude-newer` two weeks before locking | An unpinned extra only | Reproducible, supply-chain-conservative CI, matching the convention used for the v0.4 lab |

## Blocked pending approval (not implemented)

| # | Item | Why it is blocked |
|---|---|---|
| B1 | Any network transport (Streamable HTTP or SSE), hosted connectors, remote clients | Requires principal authentication, TLS and an exposure model. This changes the security surface |
| B2 | Any non-read-only capability (write, transmit, control, install, execute, scan, enroll) | Physical or operational consequences; would need a separate safety case |
| B3 | Real devices, buses or services as adapters | Real equipment access; master-prompt prohibition |
| B4 | Cryptographic entity or principal identity | Key management and an enrollment design are needed first |
| B5 | Persistent storage of observations or audit records | Retention, privacy and file-permission policy must be decided |
| B6 | Promotion of `v0` schemas to a stable public API | Depends on the review of ADR 0002 (PR #9) |
