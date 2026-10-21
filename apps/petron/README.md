# PETRON V0.1 (experimental)

Supervised engineering runtime: a provider proposes a plan, a human approves that exact plan,
a gateway authorizes every step, code runs only in a measured sandbox, and the runtime — not
the model — verifies the result and records the evidence.

Independent of PexisOS. The only link is an optional, read-only MCP client of the PexisOS
Agent Fabric (synthetic demo data).

> With the deterministic provider PETRON demonstrates infrastructure, not autonomy.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
export PETRON_HOME=$PWD/.petron

petron sandbox probe                       # measures Docker isolation; exit 1 = execution disabled
petron experiment e001                     # creates + plans E001, stops at awaiting_approval
petron mission show petron-e001            # review the plan
petron mission approve petron-e001 --plan-sha256 <hex> --approver "<your name>"
petron mission run petron-e001
petron mission report petron-e001
petron events verify
```

Requirements: Python ≥ 3.11. Execution also needs a running Docker daemon and the pinned image
(`docker pull mirror.gcr.io/library/python@sha256:cce12316…ff8`). Without them, PETRON still plans
and gates, but every mission that needs execution fails closed.

Optional: `pip install -e '.[pexisos]'` and
`petron fabric probe --principal demo:principal:analyst --pexislink-src /path/to/PexisOS/src`.

Real model (opt-in, costs money): `pip install -e '.[anthropic]'`, set `ANTHROPIC_API_KEY`,
use a spec with `"provider": {"kind": "anthropic"}` and `budget.max_model_calls ≥ 1`, then
`petron mission plan <id> --allow-real-model`.

## Tests

```bash
PYTHONPATH=src:tests python -m unittest discover -s tests -t tests
# Docker tests run only if the probe passes; PexisOS tests need mcp and PETRON_PEXISLINK_SRC.
```

Docs: `docs/ARCHITECTURE.md`, `docs/THREAT-MODEL.md`, `docs/adr/`, `docs/PREFLIGHT-2026-10-21.md`,
`docs/experiments/PETRON-E001.md`.
