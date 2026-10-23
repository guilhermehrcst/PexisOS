import os
import shutil
import tempfile
import unittest
from pathlib import Path

from petron.mission import MissionState
from petron.tools import Gateway, MissionContext
from petron.workspace import Workspace, WorkspaceError, snapshot

from support import E001, EngineCase

S = MissionState


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-ws-"))
        self.ws = Workspace.create(E001 / "project", self.tmp / "area", "work")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_is_independent_of_source(self):
        before = snapshot(E001 / "project")
        self.ws.replace_once("calc/stats.py", "    return ordered[middle]\n", "    return 0\n")
        self.assertEqual(snapshot(E001 / "project"), before)

    def test_traversal_and_absolute_paths_refused(self):
        for path in ("../x", "calc/../../x", "/etc/passwd", "calc\\stats.py", ".git/config", ""):
            with self.assertRaises(WorkspaceError, msg=path):
                self.ws.resolve(path)

    def test_symlink_hop_refused(self):
        os.symlink("/etc", self.ws.root / "link")
        with self.assertRaises(WorkspaceError):
            self.ws.read_text("link/passwd")

    def test_symlinked_source_tree_refused(self):
        bad = self.tmp / "bad"
        shutil.copytree(E001 / "project", bad)
        os.symlink("/etc/passwd", bad / "calc" / "leak.py")
        with self.assertRaises(WorkspaceError):
            Workspace.create(bad, self.tmp / "area2", "work")

    def test_replace_requires_unique_match(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.ws.replace_once("calc/stats.py", "raise ValueError", "pass")
        self.assertEqual(caught.exception.code, "old_text_not_unique")
        with self.assertRaises(WorkspaceError) as caught:
            self.ws.replace_once("calc/stats.py", "does-not-exist", "pass")
        self.assertEqual(caught.exception.code, "old_text_not_found")


class GatewayTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.mission_id, self.plan_sha = self.planned()
        self.ws = Workspace.create(self.source, self.tmp / "area", "work")
        self.ctx = MissionContext(self.engine.store, self.engine.store.spec(self.mission_id), self.ws,
                                  self.sandbox, self.plan_sha)
        self.gateway = Gateway()

    def running(self, approve: bool = True):
        if approve:
            self.engine.approve(self.mission_id, self.plan_sha, "tester", "unit-test")
        self.engine.store.transition(self.mission_id, S.AWAITING_APPROVAL, S.RUNNING)

    def denied(self, tool, args):
        outcome = self.gateway.call(self.ctx, tool, args)
        self.assertFalse(outcome["ok"])
        return outcome["error"]

    def test_denied_while_not_running(self):
        self.assertEqual(self.denied("list_files", {}), "mission_not_running")

    def test_unknown_tool_and_argument_escalation(self):
        self.running()
        self.assertEqual(self.denied("shell", {"command": "id"}), "unknown_tool")
        self.assertEqual(self.denied("read_file", {"path": "calc/stats.py", "principal": "root"}),
                         "invalid_arguments")
        self.assertEqual(self.denied("pexisos.list_entities", {}), "tool_not_permitted")

    def test_protected_and_unlisted_paths_not_writable(self):
        self.running()
        self.assertEqual(self.denied("replace_in_file", {"path": "tests/test_stats.py", "old": "2.5", "new": "3"}),
                         "path_not_writable")
        self.assertEqual(self.denied("replace_in_file", {"path": "calc/__init__.py", "old": "S", "new": "s"}),
                         "path_not_writable")

    def test_execute_requires_matching_approval(self):
        self.running(approve=False)
        self.assertEqual(self.denied("run_tests", {}), "approval_missing")
        self.assertEqual(self.sandbox.runs, [])

    def test_tool_budget(self):
        self.running()
        self.ctx.tool_calls = self.ctx.spec.budget["max_tool_calls"]
        self.assertEqual(self.denied("list_files", {}), "tool_budget_exhausted")

    def test_every_decision_is_recorded(self):
        self.running()
        self.gateway.call(self.ctx, "list_files", {})
        self.denied("shell", {})
        decisions = [e["payload"] for e in self.engine.store.events(self.mission_id) if e["kind"] == "tool.authorization"]
        self.assertEqual([d["decision"] for d in decisions], ["allow", "deny"])
        self.assertEqual(decisions[1]["tool"], "<unknown>")  # raw untrusted names are not logged


if __name__ == "__main__":
    unittest.main()
