"""Helpers for the security-hardening regression tests (kept apart from support.py)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from petron.engine import Engine
from petron.providers.base import PlanProposal

from support import FakeSandbox, e001_spec

# Synthetic credentials only. They match PETRON's redaction patterns but are not real.
FAKE_ASSIGNED = "hunter2hunter2"
FAKE_ANTHROPIC = "sk-ant-api03-SYNTHETICKEY0123456789"
FAKE_GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


class CapturingProvider:
    """Stands in for a real provider: records exactly what PETRON would have sent, never networks."""

    kind = "anthropic"
    simulated = False

    def __init__(self):
        self.requests = []

    def propose_plan(self, request):
        self.requests.append(request)
        plan = {"schema": "petron.plan.v0", "rationale": "run the tests",
                "steps": [{"tool": "run_tests", "args": {}}]}
        return PlanProposal(plan, {"provider": "anthropic", "simulated": False})


def real_provider_spec(source: Path, **changes) -> dict:
    spec = e001_spec(source, **changes)
    spec["provider"] = {"kind": "anthropic", "model": "claude-opus-5-5"}
    spec["budget"]["max_model_calls"] = 1
    return spec


class HardeningCase(unittest.TestCase):
    """An engine whose 'real' provider is a capturing fake and whose sandbox never runs code."""

    def setUp(self):
        from support import E001
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-hard-"))
        self.source = self.tmp / "source"
        shutil.copytree(E001 / "project", self.source)
        self.provider = CapturingProvider()
        self.sandbox = FakeSandbox(True)
        self.engine = Engine(self.tmp / "home", sandbox_factory=lambda spec: self.sandbox,
                             provider_factory=lambda config, allow_real_model=False: self.provider)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self.engine.close)

    def db_bytes(self) -> bytes:
        """Everything SQLite wrote for this home: main file and write-ahead log."""
        data = b""
        for suffix in ("", "-wal", "-shm"):
            path = self.tmp / "home" / f"petron.sqlite3{suffix}"
            if path.exists():
                data += path.read_bytes()
        return data
