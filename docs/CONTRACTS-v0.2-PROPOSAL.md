# Contracts v0.2: proposal for resolving I1 and I2

**Status:** PROPOSED. Specification and analysis only; nothing here is implemented. The v0.1 tool
set (`list_entities`, `describe_entity`, `list_capabilities`, `read_observations`,
`explain_assessment`) is unchanged. No existing tool may be removed or renamed without an approved
migration contract (this document proposes one). Promotion of any schema to a stable API stays
blocked (B6).

## 1. Problem, as measured

| # | Finding | Evidence |
|---|---|---|
| I1 | `read_observations` uses a conditional schema (`if mode = latest then not(cursor or limit)`). The OpenAI Agents SDK's opt-in strict conversion marks every property required without making optional ones nullable, and keeps `if/then`. The resulting schema cannot be satisfied for `mode=latest` | `test_T07_sdk_strict_schema_conversion_is_a_known_incompatibility` (a jsonschema check over every latest variant) |
| I2 | A strict-mode model may send `null` for an optional field. The core rejects it as `invalid_arguments`, exactly as its published schema says | T07 `null_for_optional_arguments`, and C09 through Codex |

Root cause: **one tool carries two operations** with different capabilities (`observe` and
`read_history`) and different argument sets, so the schema needs a conditional. Strict converters
that require every property and forbid conditionals then cannot express it. I2 is independent: the
contract has no way to say "absent" in a strict-mode payload, where every property is present.

## 2. Options evaluated

| Option | I1 | I2 | Compatibility | Assessment |
|---|---|---|---|---|
| A. Keep `read_observations`; drop the `if/then` and silently ignore `cursor`/`limit` for latest | resolved | no | Additive in the schema, but **changes semantics**: a forbidden argument becomes ignored | Rejected. Silently ignoring input violates the closed-schema principle and hides model mistakes |
| B. Keep `read_observations`; make `cursor`/`limit` nullable (`null` ≡ absent) and drop `if/then` | partly (latest still cannot carry a non-null cursor or limit, but the strict schema is satisfiable) | resolved | Changes accepted inputs of an existing name | Weaker. The capability union `observe|read_history` stays on one tool |
| **C. Split into `read_latest` and `read_history`, each with a flat schema, and `null` ≡ absent for optional fields of the new tools only** | resolved (no conditional exists) | resolved for the new tools | **Additive:** new names, old name unchanged | **Recommended** |
| D. Split only, with no nullable fields | resolved for `read_latest`; `read_history` under strict conversion would require a non-null `cursor` on the first page | no | Additive | Insufficient |

### Proposed schemas (option C)

```json
read_latest  { "entity_id": <id> }                                        required: [entity_id]
             additionalProperties: false            capability: observe
read_history { "entity_id": <id>,
               "cursor": {"type": ["string","null"], "pattern": "^[0-9]{1,18}$"},
               "limit":  {"type": ["integer","null"], "minimum": 1, "maximum": 50} }
             required: [entity_id]                  additionalProperties: false
             capability: read_history
```

Semantics: `null` for `cursor` or `limit` means exactly "absent", and nothing else (no default
inference). Results are byte-identical to `read_observations` with `mode=latest` and `mode=history`
respectively. Both go through the same `Session.call` path, policy decision and audit (the audit
`operation` is the new tool name). Error codes are unchanged.

Strict-conversion check (the acceptance criterion): after the measured OpenAI strict conversion
(all properties required, no nullability added), `read_latest` has one required property, and
`read_history` requires `cursor` and `limit` that already admit `null`. Both are satisfiable. This
must be proven by a jsonschema test, as T07 does for I1 today.

## 3. Versioning

- MCP has no per-tool versioning. **A tool name is its contract.** The semantics of an existing name
  never change. New semantics get new names.
- The tool set is versioned as a whole: `pexislink.tools.v0.1` (current five) and `pexislink.tools.v0.2`
  (`list_entities`, `describe_entity`, `list_capabilities`, `read_latest`, `read_history`,
  `explain_assessment`, plus `read_observations` while it is deprecated). The version is announced in
  the server's `instructions`/serverInfo, and asserted by tests.
- Observation payloads keep `pexislink.observation.v0`. This proposal does not touch them.

## 4. Migration contract

| Phase | Change | Default | Gate |
|---|---|---|---|
| P0 (this document) | none | v0.1 | Review of this proposal |
| P1 | Implement the v0.2 tool set behind an operator launch option `--tools v0.2`. Tests for both sets; the v0.1 tests are unchanged and keep running | **v0.1** | Approval of this proposal. CI green for both sets. Strict-conversion satisfiability test. OpenAI and Codex suites updated for v0.2 under new ids, old ids kept |
| P2 | Default becomes v0.2. `read_observations` remains, marked deprecated in its description | v0.2 | Explicit approval, release notes, at least one released version in P1 |
| P3 | `read_observations` removed from v0.2 (still available with `--tools v0.1`) | v0.2 | Explicit approval after a documented deprecation window |

Removing `--tools v0.1` entirely is out of scope and would need its own decision.

## 5. Client experience

- Two smaller, flat schemas instead of one conditional schema: they are easier for models, and every
  measured client converter can express them.
- The tool count rises from 5 to 6 (7 while deprecated) under v0.2. Discovery tests (T02, C03) must
  assert the selected set, not a hard-coded 5.
- One tool, one capability: `list_capabilities` maps directly onto tools, and an audit record names
  the exact operation.
- Name collisions across servers (I4) are unaffected. Clients still need server prefixes.

## 6. Security and invariants

There is no new capability, no write path, no change to policy, identity binding or error
sanitization. The new handlers call the same core functions as `read_observations`. Accepting `null`
widens the accepted input only for the new tools, and only as an explicit synonym for absence. It
must be validated in the core, not by the SDK, like every other argument.

## 7. Decision requested

1. Approve option C (split plus `null` ≡ absent in the new tools) or choose another.
2. Approve the P1 launch option and the default staying at v0.1 until P2.
3. Confirm the deprecation window for P3.
