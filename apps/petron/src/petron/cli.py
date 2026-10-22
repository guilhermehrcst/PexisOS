"""``petron`` command line.

    petron [--home DIR] mission create SPEC.json
    petron mission plan ID [--allow-real-model]
    petron mission approve ID (--plan-sha256 | --scope-sha256) HEX --approver NAME [--channel cli]
    petron mission run ID [--allow-real-model] [--pexislink-src DIR --fabric-python PY]
    petron mission list | show ID | log ID [--follow] | diff ID | report ID [--format md|json]
    petron mission stop ID | cancel ID | resume ID | discard ID
    petron events verify
    petron sandbox probe --image REF@sha256:...
    petron fabric probe --principal ID --pexislink-src DIR [--fabric-python PY]
    petron experiment e001 [--approver NAME] [--sandbox docker|none] [--image REF]
    petron experiment e002 [--approver NAME] [--provider deterministic|anthropic --allow-real-model]
    petron provider check --model ID      (Models API; needs ANTHROPIC_API_KEY)

Exit codes: 0 success; 1 the mission or check did not succeed; 2 invalid input or refused.
State lives in ``$PETRON_HOME`` (default ``~/.petron``). Nothing is pushed, published or deployed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .engine import Engine, EngineError
from .mission import MissionState
from .report import build_report, render_markdown
from .schema import SchemaError
from .store import StoreError

DEFAULT_IMAGE = ("mirror.gcr.io/library/python@sha256:"
                 "cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _home(args) -> Path:
    return Path(args.home or os.environ.get("PETRON_HOME") or Path.home() / ".petron")


def _load_spec(path: Path, overrides: dict | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    workspace = data.get("workspace", {})
    source = Path(workspace.get("source", ""))
    if workspace.get("source") and not source.is_absolute():
        workspace["source"] = str((path.parent / source).resolve())
    for key, value in (overrides or {}).items():
        data[key] = value
    return data


def _connector_factory(args):
    if not getattr(args, "pexislink_src", None):
        return None
    from .connectors.pexisos import FabricConfig, FabricConnector

    def factory(spec):
        return FabricConnector(FabricConfig(spec.raw["pexisos"]["principal"], Path(args.pexislink_src),
                                            args.fabric_python or sys.executable))
    return factory


def _print(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


_TERMINAL = {"completed", "failed", "cancelled"}


def _turn_text(turn: dict) -> str:
    decision, observation = turn["decision"], turn["observation"]
    result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
    summary = (result or {}).get("summary") or {}
    status = "ok" if observation.get("ok") else (observation.get("error_class") or "failed")
    if observation.get("error"):
        status += f" ({observation['error']})"
    if summary.get("tests_run") is not None:
        status += f" [tests {summary['tests_run']}, failures {summary['failures'] + summary['errors']}]"
    if observation.get("workspace_changed"):
        status += " [workspace changed]"
    action = decision.get("kind", "?") + (f" {decision['tool']}" if decision.get("tool") else "")
    why = (decision.get("rationale") or "").replace("\n", " ")[:90]
    return f"[{turn['iteration']:02d}] {action:28s} -> {status}" + (f"  | {why}" if why else "")


def _log(engine: Engine, mission_id: str, follow: bool) -> int:
    import time
    seen = 0
    while True:
        events = engine.store.events(mission_id)
        for event in events[seen:]:
            if event["kind"] == "agent.turn":
                print(_turn_text(event["payload"]), flush=True)
            elif event["kind"] == "mission.state":
                p = event["payload"]
                print(f"     state {p['from']} -> {p['to']}" + (f" ({p['reason']})" if p.get("reason") else ""),
                      flush=True)
        seen = len(events)
        if not follow or engine.store.state(mission_id).value in _TERMINAL:
            return 0
        time.sleep(1.0)


def _mission(args, engine: Engine) -> int:
    if args.action != "list" and not args.target:
        print("error: this action needs a mission id (or a spec file for create)", file=sys.stderr)
        return 2
    if args.action == "list":
        for row in engine.store.missions():
            interrupted = row["state"] == "running" and engine.store.lease_alive(row["mission_id"]) is False
            print(f"{row['mission_id']:32s} {row['state']:18s} {row['failure_reason'] or ''}"
                  + ("  [interrupted: petron mission resume]" if interrupted else ""))
        return 0
    if args.action == "create":
        _print({"mission_id": engine.create(_load_spec(Path(args.target)))})
    elif args.action == "plan":
        _print({"mission_id": args.target,
                "plan_sha256": engine.plan(args.target, allow_real_model=args.allow_real_model),
                "next": "review the plan (petron mission show), then approve it with its sha256"})
    elif args.action == "approve":
        digest = args.plan_sha256 or args.scope_sha256
        engine.approve(args.target, digest, args.approver, args.channel)
        _print({"mission_id": args.target, "approved": digest})
    elif args.action in ("run", "resume"):
        runner = engine.run if args.action == "run" else engine.resume
        state = runner(args.target, allow_real_model=args.allow_real_model)
        _log(engine, args.target, follow=False)
        _print({"mission_id": args.target, "state": state.value,
                "failure_reason": engine.store.mission(args.target)["failure_reason"]})
        return 0 if state is MissionState.COMPLETED else 1
    elif args.action == "log":
        return _log(engine, args.target, args.follow)
    elif args.action == "diff":
        artifact = engine.store.artifact(args.target, "diff.patch")
        if artifact is None:
            from .workspace import Workspace, changed_paths, snapshot, unified_diff
            area = engine.home / "workspaces" / args.target
            if not (area / "base").is_dir() or not (area / "work").is_dir():
                print("no diff recorded and no workspace present", file=sys.stderr)
                return 1
            base, work = Workspace(area / "base"), Workspace(area / "work")
            print(unified_diff(base.root, work.root, changed_paths(snapshot(base.root), snapshot(work.root))),
                  end="")
            return 0
        print(artifact["content"], end="")
    elif args.action in ("cancel", "stop"):
        engine.cancel(args.target)
        _print({"mission_id": args.target, "state": "cancelled"})
    elif args.action == "show":
        row = engine.store.mission(args.target)
        mode = json.loads(row["spec_json"]).get("mode", "plan")
        document = json.loads(row["plan_json"]) if row["plan_json"] else None
        _print({**{k: row[k] for k in ("mission_id", "state", "failure_reason", "created_at")}, "mode": mode,
                ("scope_sha256" if mode == "agent" else "plan_sha256"): row["plan_sha256"],
                ("scope" if mode == "agent" else "plan"): document})
    elif args.action == "report":
        report = build_report(engine.store, args.target)
        print(render_markdown(report) if args.format == "md" else json.dumps(report, indent=2, sort_keys=True))
    elif args.action == "discard":
        engine.discard_workspaces(args.target)
        _print({"mission_id": args.target, "workspaces": "discarded"})
    return 0


def _experiment_e001(args, engine: Engine) -> int:
    overrides = {"sandbox": {"backend": "none"}} if args.sandbox == "none" else \
        {"sandbox": {"backend": "docker", "image": args.image}}
    spec = _load_spec(FIXTURES / "e001" / "mission.json", overrides)
    if args.mission_id:
        spec["mission_id"] = args.mission_id
    mission_id = engine.create(spec)
    plan_sha256 = engine.plan(mission_id)
    if not args.approver:
        _print({"mission_id": mission_id, "state": "awaiting_approval", "plan_sha256": plan_sha256,
                "next": f"petron mission approve {mission_id} --plan-sha256 {plan_sha256} --approver NAME"})
        return 0
    engine.approve(mission_id, plan_sha256, args.approver, "e001-harness")
    state = engine.run(mission_id)
    report = build_report(engine.store, mission_id)
    print(render_markdown(report))
    return 0 if state is MissionState.COMPLETED else 1


def _experiment_e002(args, engine: Engine) -> int:
    name = "mission-claude.json" if args.provider == "anthropic" else "mission.json"
    overrides = {"sandbox": {"backend": "none"}} if args.sandbox == "none" else \
        {"sandbox": {"backend": "docker", "image": args.image}}
    spec = _load_spec(FIXTURES / "e002" / name, overrides)
    if args.mission_id:
        spec["mission_id"] = args.mission_id
    mission_id = engine.create(spec)
    scope_sha256 = engine.plan(mission_id)
    if not args.approver:
        _print({"mission_id": mission_id, "state": "awaiting_approval", "scope_sha256": scope_sha256,
                "next": [f"petron mission show {mission_id}",
                         f"petron mission approve {mission_id} --scope-sha256 {scope_sha256} --approver NAME",
                         f"petron mission run {mission_id}"
                         + (" --allow-real-model" if args.provider == "anthropic" else "")]})
        return 0
    engine.approve(mission_id, scope_sha256, args.approver, "e002-harness")
    state = engine.run(mission_id, allow_real_model=args.allow_real_model)
    print(render_markdown(build_report(engine.store, mission_id)))
    return 0 if state is MissionState.COMPLETED else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="petron", description="PETRON supervised engineering runtime (V0.2)")
    parser.add_argument("--home", help="state directory (default $PETRON_HOME or ~/.petron)")
    sub = parser.add_subparsers(dest="command", required=True)

    mission = sub.add_parser("mission")
    mission.add_argument("action", choices=["create", "plan", "approve", "run", "resume", "cancel", "stop", "show",
                                            "list", "log", "diff", "report", "discard"])
    mission.add_argument("target", nargs="?", help="spec file (create) or mission id")
    mission.add_argument("--scope-sha256")
    mission.add_argument("--follow", action="store_true", help="log: keep printing until the mission ends")
    mission.add_argument("--allow-real-model", action="store_true")
    mission.add_argument("--plan-sha256")
    mission.add_argument("--approver")
    mission.add_argument("--channel", default="cli")
    mission.add_argument("--format", choices=["md", "json"], default="md")
    mission.add_argument("--pexislink-src")
    mission.add_argument("--fabric-python")

    events = sub.add_parser("events")
    events.add_argument("action", choices=["verify"])

    sandbox = sub.add_parser("sandbox")
    sandbox.add_argument("action", choices=["probe"])
    sandbox.add_argument("--image", default=DEFAULT_IMAGE)

    fabric = sub.add_parser("fabric")
    fabric.add_argument("action", choices=["probe"])
    fabric.add_argument("--principal", required=True)
    fabric.add_argument("--pexislink-src", required=True)
    fabric.add_argument("--fabric-python")

    experiment = sub.add_parser("experiment")
    experiment.add_argument("name", choices=["e001", "e002"])
    experiment.add_argument("--provider", choices=["deterministic", "anthropic"], default="deterministic")
    experiment.add_argument("--allow-real-model", action="store_true")
    experiment.add_argument("--approver", help="who approves the scripted plan (recorded verbatim)")
    experiment.add_argument("--sandbox", choices=["docker", "none"], default="docker")
    experiment.add_argument("--image", default=DEFAULT_IMAGE)
    experiment.add_argument("--mission-id")

    provider = sub.add_parser("provider")
    provider.add_argument("action", choices=["check"])
    provider.add_argument("--model", default="claude-opus-5-5")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mission" and args.action == "approve" and not (
            (args.plan_sha256 or args.scope_sha256) and args.approver):
        print("error: approve needs --plan-sha256 (or --scope-sha256) and --approver", file=sys.stderr)
        return 2
    if args.command == "provider":
        from .providers import ProviderError
        from .providers.anthropic_provider import check_model
        try:
            _print({"model": check_model(args.model), "verified_via": "Models API"})
        except ProviderError as error:
            print(f"error: {error.code}", file=sys.stderr)
            return 1
        return 0
    if args.command == "sandbox":
        from .sandbox import DockerSandbox
        try:
            report = DockerSandbox(args.image).probe()
        except ValueError:
            print("error: image must be pinned by digest", file=sys.stderr)
            return 2
        _print(report.to_dict())
        return 0 if report.ok else 1
    if args.command == "fabric":
        from .connectors.pexisos import READ_ONLY_TOOLS, ConnectorError, FabricConfig, FabricConnector
        try:
            connector = FabricConnector(FabricConfig(args.principal, Path(args.pexislink_src),
                                                     args.fabric_python or sys.executable))
            outcome = connector.run([("list_entities", {})])
        except ConnectorError as error:
            print(f"error: {error.code}", file=sys.stderr)
            return 1
        _print({**outcome, "allowlist": sorted(READ_ONLY_TOOLS), "simulated": False})
        return 0
    engine = Engine(_home(args), connector_factory=_connector_factory(args) if args.command == "mission" else None)
    try:
        if args.command == "events":
            ok, bad = engine.store.verify_chain()
            _print({"chain_ok": ok, "first_bad_seq": bad})
            return 0 if ok else 1
        if args.command == "experiment":
            return _experiment_e002(args, engine) if args.name == "e002" else _experiment_e001(args, engine)
        return _mission(args, engine)
    except (EngineError, StoreError) as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    except (SchemaError, ValueError, OSError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
