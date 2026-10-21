"""Shared test helpers. ``FakeSandbox`` never executes fixture code: it inspects files only."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from petron.engine import Engine
from petron.sandbox import ProbeReport, RunResult, SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]
E001 = ROOT / "fixtures" / "e001"
FIX_MARKER = "(ordered[middle - 1] + ordered[middle]) / 2"


def e001_spec(source: Path, **changes) -> dict:
    spec = json.loads((E001 / "mission.json").read_text())
    spec["workspace"]["source"] = str(source)
    spec["sandbox"] = {"backend": "none"}
    for key, value in changes.items():
        spec[key] = value
    return spec


class FakeSandbox:
    backend = "fake"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.runs: list[Path] = []

    def probe(self) -> ProbeReport:
        return ProbeReport("fake", self.ok, {"fake": self.ok}, reason=None if self.ok else "fake_unavailable")

    def run(self, workspace: Path, argv, timeout_s: int) -> RunResult:
        if not self.ok:
            raise SandboxUnavailable("fake_unavailable")
        self.runs.append(Path(workspace))
        code = (Path(workspace) / "calc" / "stats.py").read_text()
        tests = (Path(workspace) / "tests" / "test_stats.py").read_text()
        passing = FIX_MARKER in code and "2.5)" in tests
        return RunResult(0 if passing else 1, "", "OK\n" if passing else "FAILED (failures=1)\n", False)


class EngineCase(unittest.TestCase):
    sandbox_ok = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-test-"))
        self.source = self.tmp / "source"
        shutil.copytree(E001 / "project", self.source)
        self.sandbox = FakeSandbox(self.sandbox_ok)
        self.engine = Engine(self.tmp / "home", sandbox_factory=lambda spec: self.sandbox)

    def tearDown(self):
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spec(self, script: str = "e001_fix_median", **changes) -> dict:
        spec = e001_spec(self.source, **changes)
        spec["provider"] = {"kind": "deterministic", "script": script}
        return copy.deepcopy(spec)

    def planned(self, script: str = "e001_fix_median", **changes) -> tuple[str, str]:
        mission_id = self.engine.create(self.spec(script, **changes))
        return mission_id, self.engine.plan(mission_id)

    def kinds(self, mission_id: str) -> list[str]:
        return [event["kind"] for event in self.engine.store.events(mission_id)]
