"""V0.2 engineering tools: bounded, confined, atomic."""

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from petron.workspace import Workspace, WorkspaceError

from support import E002, AgentCase

MERGE = "intervals/merge.py"


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-tools-"))
        self.ws = Workspace.create(E002 / "project", self.tmp / "area", "work")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_search_is_literal_and_bounded(self):
        found = self.ws.search("def merge(")
        self.assertEqual([(m["path"], m["line"]) for m in found["matches"]], [(MERGE, 4)])
        self.assertEqual(self.ws.search(".*")["matches"], [])          # not a regex
        self.assertTrue(self.ws.search("e", max_results=2)["truncated"])

    def test_search_prefix_is_confined(self):
        for prefix in ("../", "/etc", "intervals/../.."):
            with self.assertRaises(WorkspaceError, msg=prefix):
                self.ws.search("x", prefix)
        self.assertEqual({m["path"] for m in self.ws.search("merge", "tests")["matches"]}, {"tests/test_merge.py"})

    def test_ranged_read_reports_the_whole_file_hash(self):
        whole = (self.ws.root / MERGE).read_text()
        part = self.ws.read_lines(MERGE, 4, 6)
        self.assertEqual(part["content"], "".join(whole.splitlines(keepends=True)[3:6]))
        self.assertEqual(part["sha256"], hashlib.sha256(whole.encode()).hexdigest())
        self.assertEqual(part["total_lines"], len(whole.splitlines()))
        with self.assertRaises(WorkspaceError):
            self.ws.read_lines(MERGE, 500, 600)

    def test_edit_refuses_a_stale_version_and_returns_the_new_hash(self):
        sha = self.ws.read_lines(MERGE)["sha256"]
        new_sha = self.ws.replace_once(MERGE, "start < result", "start <= result", expected_sha256=sha)
        self.assertEqual(new_sha, self.ws.read_lines(MERGE)["sha256"])
        with self.assertRaises(WorkspaceError) as caught:
            self.ws.replace_once(MERGE, "sorted", "x", expected_sha256=sha)   # sha is now stale
        self.assertEqual(caught.exception.code, "stale_file_version")
        self.assertEqual([p.name for p in (self.ws.root / "intervals").iterdir() if "petron-tmp" in p.name], [])


class GatewayToolTests(AgentCase):
    def test_show_diff_and_search_through_the_gateway(self):
        mission_id = self.start()
        self.engine.run(mission_id)
        turns = self.turns(mission_id)
        search = [t for t in turns if t["decision"].get("tool") == "search_code"][0]["observation"]
        self.assertEqual(search["result"]["matches"][0]["path"], MERGE)
        reads = [t for t in turns if t["decision"].get("tool") == "read_file"]
        self.assertTrue(all(len(t["observation"]["result"]["sha256"]) == 64 for t in reads))
        edits = [t for t in turns if t["decision"].get("tool") == "replace_in_file"]
        self.assertTrue(all("expected_sha256" in t["decision"]["args"] for t in edits))


if __name__ == "__main__":
    unittest.main()
