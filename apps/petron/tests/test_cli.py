"""CLI flow for agent missions (no Docker needed: without a sandbox the mission must fail closed)."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from petron.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="petron-cli-"))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def petron(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home), *args])
        return code, out.getvalue(), err.getvalue()

    def test_agent_mission_flow(self):
        code, out, _ = self.petron("experiment", "e002", "--sandbox", "none")
        self.assertEqual(code, 0)
        scope = json.loads(out)["scope_sha256"]
        code, _, err = self.petron("mission", "run", "petron-e002")
        self.assertEqual((code, err.strip()), (2, "error: approval_missing"))
        code, out, _ = self.petron("mission", "show", "petron-e002")
        shown = json.loads(out)
        self.assertEqual((shown["mode"], shown["scope_sha256"]), ("agent", scope))
        self.assertIn("replace_in_file", shown["scope"]["tools"])
        self.assertEqual(self.petron("mission", "approve", "petron-e002", "--scope-sha256", scope,
                                     "--approver", "tester")[0], 0)
        code, out, _ = self.petron("mission", "run", "petron-e002")
        self.assertEqual(code, 1)                                   # no sandbox: cannot succeed
        self.assertIn("execution_unavailable", out)
        self.assertIn("agent_stopped:impossible", out)
        code, out, _ = self.petron("mission", "log", "petron-e002")
        self.assertIn("[01] tool_call list_files", out)
        code, out, _ = self.petron("mission", "list")
        self.assertIn("petron-e002", out)
        self.assertEqual(self.petron("mission", "diff", "petron-e002")[0], 0)

    def test_real_model_needs_explicit_opt_in(self):
        code, out, _ = self.petron("experiment", "e002", "--provider", "anthropic", "--sandbox", "none")
        mission = json.loads(out)
        self.assertIn("--allow-real-model", mission["next"][-1])
        self.petron("mission", "approve", mission["mission_id"], "--scope-sha256", mission["scope_sha256"],
                    "--approver", "tester")
        code, _, err = self.petron("mission", "run", mission["mission_id"])
        self.assertEqual((code, err.strip()), (2, "error: provider_error:real_model_not_authorized"))
        code, out, _ = self.petron("mission", "show", mission["mission_id"])
        self.assertEqual(json.loads(out)["state"], "awaiting_approval")   # refused before running


if __name__ == "__main__":
    unittest.main()
