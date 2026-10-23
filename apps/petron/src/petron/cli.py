"""``petron`` command line.

    petron [--home DIR] mission create SPEC.json
    petron mission plan ID [--allow-real-model]
    petron mission approve ID --plan-sha256 HEX --approver NAME [--channel cli]
    petron mission run ID [--pexislink-src DIR --pexislink-src-sha256 HEX --fabric-python PY]
    petron mission cancel ID | show ID | report ID [--format md|json] | discard ID
    petron events verify
    petron sandbox probe --image REF@sha256:...
    petron fabric digest --pexislink-src DIR
    petron fabric probe --principal ID --pexislink-src DIR --pexislink-src-sha256 HEX [--fabric-python PY]
    petron experiment e001 [--approver NAME] [--sandbox docker|none] [--image REF]

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
from .workspace import WorkspaceError
from .mission import MissionState
from .redact import redact
from .report import build_report, render_markdown
from .schema import SchemaError
from .store import StoreError

DEFAULT_IMAGE = ("mirror.gcr.io/library/python@sha256:"
                 "cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8")


def _fixtures_dir() -> Path:
    """Installed wheel: ``petron/fixtures`` (package data). Source checkout: ``apps/petron/fixtures``."""
    packaged = Path(__file__).resolve().parent / "fixtures"
    return packaged if (packaged / "e001" / "mission.json").is_file() else Path(__file__).resolve().parents[2] / "fixtures"


FIXTURES = _fixtures_dir()


CHAIN_NOTE = ("unkeyed hash chain: detects accidental damage, edits and deletions in the middle, and (via the "
              "local anchor file, or a head you recorded elsewhere) truncation of the newest events. It is not "
              "tamper-proof: whoever can rewrite the database and the anchor can recompute everything. Record "
              "`petron events head` somewhere independent and pass it back with --expect-head.")


def _home(args) -> Path:
    return Path(args.home or os.environ.get("PETRON_HOME") or Path.home() / ".petron")


def _load_spec(path: Path, overrides: dict | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SchemaError("", "expected object")
    workspace = data.get("workspace")
    # Types are checked before they are used; anything else flows on to MissionSpec.parse, which
    # reports it with a domain error instead of a traceback.
    if isinstance(workspace, dict) and isinstance(workspace.get("source"), str) and workspace["source"]:
        source = Path(workspace["source"])
        if not source.is_absolute():
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
                                            args.fabric_python or sys.executable,
                                            pexislink_src_sha256=args.pexislink_src_sha256))
    return factory


def _print(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _mission(args, engine: Engine) -> int:
    if args.action == "create":
        _print({"mission_id": engine.create(_load_spec(Path(args.target)))})
    elif args.action == "plan":
        _print({"mission_id": args.target,
                "plan_sha256": engine.plan(args.target, allow_real_model=args.allow_real_model),
                "next": "review the plan (petron mission show), then approve it with its sha256"})
    elif args.action == "approve":
        engine.approve(args.target, args.plan_sha256, args.approver, args.channel)
        _print({"mission_id": args.target, "approved": args.plan_sha256})
    elif args.action == "run":
        state = engine.run(args.target)
        _print({"mission_id": args.target, "state": state.value})
        return 0 if state is MissionState.COMPLETED else 1
    elif args.action == "cancel":
        engine.cancel(args.target)
        _print({"mission_id": args.target, "state": "cancelled"})
    elif args.action == "show":
        row = engine.store.mission(args.target)
        _print({**{k: row[k] for k in ("mission_id", "state", "plan_sha256", "failure_reason", "created_at")},
                "plan": redact(json.loads(row["plan_json"])) if row["plan_json"] else None})
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="petron", description="PETRON V0.1 supervised engineering runtime")
    parser.add_argument("--home", help="state directory (default $PETRON_HOME or ~/.petron)")
    sub = parser.add_subparsers(dest="command", required=True)

    mission = sub.add_parser("mission")
    mission.add_argument("action", choices=["create", "plan", "approve", "run", "cancel", "show", "report",
                                            "discard"])
    mission.add_argument("target", help="spec file (create) or mission id")
    mission.add_argument("--allow-real-model", action="store_true")
    mission.add_argument("--plan-sha256")
    mission.add_argument("--approver")
    mission.add_argument("--channel", default="cli")
    mission.add_argument("--format", choices=["md", "json"], default="md")
    mission.add_argument("--pexislink-src")
    mission.add_argument("--pexislink-src-sha256", help="operator's pin of the MCP server source tree")
    mission.add_argument("--fabric-python")

    events = sub.add_parser("events")
    events.add_argument("action", choices=["verify", "head"])
    events.add_argument("--expect-head", metavar="SEQ:HASH",
                        help="a head you recorded elsewhere (petron events head); detects truncation or rollback")

    sandbox = sub.add_parser("sandbox")
    sandbox.add_argument("action", choices=["probe"])
    sandbox.add_argument("--image", default=DEFAULT_IMAGE)

    fabric = sub.add_parser("fabric")
    fabric.add_argument("action", choices=["probe", "digest"])
    fabric.add_argument("--principal")
    fabric.add_argument("--pexislink-src", required=True)
    fabric.add_argument("--pexislink-src-sha256", help="operator's pin of the MCP server source tree")
    fabric.add_argument("--fabric-python")

    experiment = sub.add_parser("experiment")
    experiment.add_argument("name", choices=["e001"])
    experiment.add_argument("--approver", help="who approves the scripted plan (recorded verbatim)")
    experiment.add_argument("--sandbox", choices=["docker", "none"], default="docker")
    experiment.add_argument("--image", default=DEFAULT_IMAGE)
    experiment.add_argument("--mission-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mission" and args.action == "approve" and not (args.plan_sha256 and args.approver):
        print("error: approve needs --plan-sha256 and --approver", file=sys.stderr)
        return 2
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
        from .connectors.pexisos import (READ_ONLY_TOOLS, ConnectorError, FabricConfig, FabricConnector,
                                         digest_source)
        if args.action == "digest":
            try:
                _print({"pexislink_src_sha256": digest_source(Path(args.pexislink_src)),
                        "note": "pin a digest you obtained from a trusted, reviewed source; pinning one "
                                "computed here only freezes whatever is on disk now"})
            except ConnectorError as error:
                print(f"error: {error.code}", file=sys.stderr)
                return 1
            return 0
        if not args.principal:
            print("error: fabric probe needs --principal", file=sys.stderr)
            return 2
        try:
            connector = FabricConnector(FabricConfig(args.principal, Path(args.pexislink_src),
                                                     args.fabric_python or sys.executable,
                                                     pexislink_src_sha256=args.pexislink_src_sha256))
            outcome = connector.run([("list_entities", {})])
        except ConnectorError as error:
            print(f"error: {error.code}", file=sys.stderr)
            return 1
        _print({**outcome, "allowlist": sorted(READ_ONLY_TOOLS), "simulated": False})
        return 0
    engine = Engine(_home(args), connector_factory=_connector_factory(args) if args.command == "mission" else None)
    try:
        if args.command == "events":
            if args.action == "head":
                _print(engine.store.head() or {"seq": 0, "hash": None})
                return 0
            expected = None
            if args.expect_head:
                seq, _, digest = args.expect_head.partition(":")
                if not (seq.isdigit() and len(digest) == 64):
                    print("error: --expect-head must be SEQ:HASH (64 hex)", file=sys.stderr)
                    return 2
                expected = {"seq": int(seq), "hash": digest}
            ok, bad = engine.store.verify_chain(expected_head=expected)
            _print({"chain_ok": ok, "first_bad_seq": bad, "note": CHAIN_NOTE})
            return 0 if ok else 1
        if args.command == "experiment":
            return _experiment_e001(args, engine)
        return _mission(args, engine)
    except (EngineError, StoreError, WorkspaceError) as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    except (SchemaError, ValueError, OSError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
