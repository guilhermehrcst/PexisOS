# PETRON V0.1 — M0 Preflight (2026-10-21)

Observed state, measured in the development container. Where it differs from the
target machine, the target machine wins and must be re-measured there.
`VALIDATED_ONCE != VALID_FOREVER`.

## Host

| Item | Observed | Target (declared by the owner, not measured) |
|---|---|---|
| CPU | 4 vCPU (cloud VM, kernel 6.18) | AMD FX-6300, 6 cores |
| RAM | 16 GiB | 8 GiB |
| GPU | none | none (CPU-only) |
| User | root (uid 0) inside an ephemeral container | regular user |
| Python | 3.11.17, 3.12.3, 3.13.16 | ≥ 3.11 assumed |
| SQLite | 3.45.1 | unknown |

Consequence: PETRON must stay CPU-light and must not run a local LLM. Every component in
V0.1 is the Python standard library plus optional extras.

## Repositories

| Repository | State | PETRON relation |
|---|---|---|
| `guilhermehrcst/PexisOS` (`/home/user/pexisos`) | `main` = `db097c8`; checkout on `feat/pexisos-console-v1` @ `de4af68`, clean | read-only reference; **not modified** |
| `/home/user/Pexislink` | `compat-lab-review-fixes` @ `118bf79`, clean | not used |
| PETRON | **no repository existed** (no remote, no local) | created at `/home/user/petron` as a separate local Git repo, **no remote** |

Open PexisOS PRs re-confirmed through the GitHub API on 2026-10-21:

| PR | Head | Base | Draft |
|---|---|---|---|
| #6 docs Component X v3 | `ade4f99` | main | no |
| #13 multi-provider compat lab | `ea9e925` | main | yes |
| #21 open-core IP boundary D1 | `782ff60` (remote) | `plan/open-core-ip-boundary-d1-v01` | no |
| #22 Console V1 | `de4af68` | main | yes |

PETRON touches none of them.

## Agent Fabric contract (PexisOS `main` @ `db097c8`)

- Transport: `pexislink fabric mcp-stdio --principal ID [--policy FILE]`, stdio only.
- Principal is bound at launch by the operator; no tool takes a principal argument; every
  input schema is closed (`additionalProperties: false`).
- Tools, all read-only: `list_entities`, `describe_entity`, `list_capabilities`,
  `read_observations`, `explain_assessment`.
- Errors: `isError: true` with `{"error": {"code", "message"[, "reason"]}}`.
- World: deterministic **synthetic** demo entities (appliance, service, vehicle);
  principals `demo:principal:analyst`, `demo:principal:guest`.
- Known limits carried from the audit (2026-10-18): FAB-01 the stdio demo uses a frozen
  clock so grant expiry is not exercised; FAB-03 the audit log is in memory only.

## Isolation capabilities

| Mechanism | Observed |
|---|---|
| Docker client | 29.8.2 |
| Docker daemon | **not running at start**; started manually (`dockerd`, rootful, data root in `/tmp`). Builtin seccomp profile active, cgroup v1, runc |
| Docker rootless | not available (`rootlesskit` absent) |
| bubblewrap / nsjail / firejail | absent |
| unprivileged user namespaces | available (`unshare --user` works) |
| image | `python:3.11-slim` pulled through `mirror.gcr.io` (Docker Hub answered 429), pinned digest `sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8` |

Measured hardened container (`--network none --cap-drop ALL --security-opt no-new-privileges
--read-only --user 65534:65534 --pids-limit 64 --memory 256m`): uid/gid 65534, effective
capabilities `0`, outbound connection fails, root filesystem not writable.

Decision: isolated execution is **possible here** through Docker with those flags, and
PETRON re-measures it before every enablement (`petron sandbox probe`). When the probe fails
(no daemon, image missing, a check fails) execution is disabled — there is no host fallback.
The rootful daemon is a residual risk recorded in the threat model.

## Credentials and network

- `ANTHROPIC_API_KEY`: **not set**. `ANTHROPIC_BASE_URL` is set by the hosting environment;
  PETRON never inherits it implicitly. A Claude Code subscription or session is **not** treated
  as API credit. Real-model test: **NOT_RUN** (no credential, no budget authorization).
- Egress goes through a policy proxy; PyPI is reachable; many hosts are blocked.

## Persistence warning

This container is ephemeral. The PETRON repository has no remote by instruction. Unless the
owner authorizes a remote (or copies the Git bundle), the code is lost when the container is
reclaimed.
