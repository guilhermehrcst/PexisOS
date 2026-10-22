# PETRON V0.2 (experimental)

Supervised engineering runtime. Two mission modes:

- **plan** (V0.1): a provider proposes a static plan; a human approves that exact plan.
- **agent** (V0.2): a human approves a *scope* (tools, writable paths, limits, budget); inside it
  the agent decides one action at a time, observes the real result and adapts.

In both, a gateway authorizes every action, code runs only in a measured sandbox, and the
runtime — not the model — verifies the result and records the evidence.

PETRON is a component of the PexisOS monorepo (`apps/petron/`), operationally independent of
the PexisOS core (`src/pexislink`): neither imports the other, neither depends on the other.
The only link is an optional, read-only MCP client of the PexisOS Agent Fabric (synthetic demo
data). See `docs/adr/ADR-0002-monorepo.md`.

> With the deterministic providers PETRON demonstrates infrastructure and loop mechanics, not
> autonomy. No real-model run has been performed.

## Quick start (from the repository root)

```bash
python3 -m venv .venv && . .venv/bin/activate   # .venv is git-ignored
pip install -e apps/petron                 # PETRON only; the PexisOS core is not installed
# State (SQLite + workspaces) goes to $PETRON_HOME, default ~/.petron, outside the repository.
# Never point PETRON_HOME inside the repository: the evidence database must not be committed.

petron sandbox probe                       # measures Docker isolation; exit 1 = execution disabled
petron experiment e001                     # creates + plans E001, stops at awaiting_approval
petron mission show petron-e001            # review the plan
petron mission approve petron-e001 --plan-sha256 <hex> --approver "<your name>"
petron mission run petron-e001
petron mission report petron-e001
petron events verify
```

Agent mode (V0.2), the adaptive repair experiment:

```bash
petron experiment e002                     # creates the mission and stops for approval
petron mission show petron-e002            # inspect the scope: tools, paths, limits
petron mission approve petron-e002 --scope-sha256 <hex> --approver "<your name>"
petron mission run petron-e002             # prints each turn: decision -> observation
petron mission log petron-e002 --follow    # from another terminal, while it runs
petron mission diff petron-e002
petron mission stop petron-e002            # interrupt (cooperative, between turns)
petron mission list                        # flags interrupted missions
petron mission resume petron-e002          # continue from the last checkpoint, if safe
```

Real model (opt-in, costs money): see `docs/experiments/PETRON-E002.md` (E002-B).

Without installing: `PYTHONPATH=apps/petron/src python -m petron …`.

Requirements: Python ≥ 3.11. Execution also needs a running Docker daemon and the pinned image
(`docker pull mirror.gcr.io/library/python@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8`).
Without them, PETRON still plans and gates, but every mission that needs execution fails closed.

Agent Fabric (optional): `pip install --require-hashes -r requirements/fabric-mcp.txt`, then
`petron fabric probe --principal demo:principal:analyst --pexislink-src src`.

Real model (opt-in, costs money): `pip install -e 'apps/petron[anthropic]'`, set
`ANTHROPIC_API_KEY`, use a spec with `"provider": {"kind": "anthropic"}` and
`budget.max_model_calls ≥ 1`, then `petron mission plan <id> --allow-real-model`.

## Tests

```bash
cd apps/petron
PYTHONPATH=src:tests python -m unittest discover -s tests -t tests -v
```

Docker tests run only if the isolation probe passes; Agent Fabric tests need `mcp` and use the
monorepo's `src/` (or `PETRON_PEXISLINK_SRC`). Skips are reported, never counted as passes. CI:
`.github/workflows/petron.yml` (the isolation job fails if anything is skipped).

## License

Apache-2.0, the PexisOS repository license (`LICENSE` here is an identical copy of `/LICENSE`).
Third-party components and notices: `docs/LICENSING.md`.

Docs: `docs/ARCHITECTURE.md`, `docs/THREAT-MODEL.md`, `docs/adr/`, `docs/PREFLIGHT-2026-10-21.md`
(historical: written when PETRON was a separate local repository), `docs/MONOREPO-MIGRATION.md`,
`docs/experiments/PETRON-E001.md`, `docs/experiments/PETRON-E002.md`, `docs/adr/ADR-0003-agent-loop.md`,
`docs/LICENSING.md`.
