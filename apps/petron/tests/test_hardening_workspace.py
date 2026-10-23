"""Source-tree integrity: no read can leave the authorized workspace, and the tree that was
approved is the tree that is read, copied and executed. (Review findings: planning TOCTOU,
source hash consistency.)"""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from petron.engine import EngineError
from petron.mission import MissionState
from petron.workspace import Workspace, WorkspaceError, read_tree, snapshot, tree_sha256

from hardening_support import HardeningCase, real_provider_spec
from support import E001

SECRET_MARKER = "OUTSIDE-SECRET-MUST-NEVER-REACH-A-PROVIDER"


class NoFollowReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-nofollow-"))
        self.source = self.tmp / "source"
        shutil.copytree(E001 / "project", self.source)
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text(SECRET_MARKER)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_read_tree_returns_the_bytes_that_were_hashed(self):
        contents = read_tree(self.source)
        self.assertEqual(snapshot(self.source), {p: __import__("hashlib").sha256(c).hexdigest()
                                                 for p, c in contents.items()})

    def test_symlink_swapped_in_between_listing_and_open_is_not_followed(self):
        target = self.source / "calc" / "stats.py"
        real_open = os.open
        swapped = []

        def swap_then_open(path, flags, *args, **kwargs):
            if path == "stats.py" and kwargs.get("dir_fd") is not None and not swapped:
                # The attacker wins the race exactly here: after the entry was classified as a
                # regular file, before it is opened.
                target.unlink()
                os.symlink(self.outside, target)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=swap_then_open):
            with self.assertRaises(WorkspaceError) as caught:
                read_tree(self.source)
        self.assertTrue(swapped, "the race was not exercised")
        self.assertIn(caught.exception.code, {"symlink_in_tree", "tree_changed_during_read"})

    def test_directory_swapped_for_a_symlink_is_not_followed(self):
        real_open = os.open
        outside_dir = self.tmp / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "stolen.py").write_text(SECRET_MARKER)
        swapped = []

        def swap_then_open(path, flags, *args, **kwargs):
            if path == "calc" and kwargs.get("dir_fd") is not None and not swapped:
                shutil.rmtree(self.source / "calc")
                os.symlink(outside_dir, self.source / "calc")
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=swap_then_open):
            with self.assertRaises(WorkspaceError):
                read_tree(self.source)
        self.assertTrue(swapped)

    def test_special_files_and_oversized_files_are_refused(self):
        os.mkfifo(self.source / "pipe")
        with self.assertRaises(WorkspaceError) as caught:
            read_tree(self.source)
        self.assertEqual(caught.exception.code, "non_regular_file_in_tree")
        os.unlink(self.source / "pipe")
        (self.source / "big.txt").write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(WorkspaceError) as caught:
            read_tree(self.source)
        self.assertEqual(caught.exception.code, "tree_limit")

    def test_workspace_read_and_edit_do_not_follow_a_late_symlink(self):
        ws = Workspace.create(self.source, self.tmp / "area", "work")
        os.unlink(ws.root / "calc" / "stats.py")
        os.symlink(self.outside, ws.root / "calc" / "stats.py")
        with self.assertRaises(WorkspaceError):
            ws.read_text("calc/stats.py")
        with self.assertRaises(WorkspaceError):
            ws.replace_once("calc/stats.py", SECRET_MARKER, "x")
        self.assertEqual(self.outside.read_text(), SECRET_MARKER)


class PlanningRaceTests(HardeningCase):
    """The provider must never receive bytes from outside the source tree, however the attacker
    races the planner."""

    def test_racing_symlink_swaps_never_leak_outside_content(self):
        for i in range(400):   # many files: a wide window between "validated" and "read"
            (self.source / "calc" / f"filler_{i}.py").write_text(f"VALUE = {i}\n")
        outside = self.tmp / "outside.txt"
        outside.write_text(SECRET_MARKER)
        victim = self.source / "calc" / "stats.py"
        original = victim.read_text()
        spec = real_provider_spec(self.source)
        mission_id = self.engine.create(spec)
        parsed = self.engine.store.spec(mission_id)
        stop = threading.Event()

        def attacker():
            link, regular = self.tmp / "swap-link", self.tmp / "swap-file"
            while not stop.is_set():
                os.symlink(outside, link)
                os.replace(link, victim)            # now a symlink
                regular.write_text(original)
                os.replace(regular, victim)         # and back to a regular file

        thread = threading.Thread(target=attacker, daemon=True)
        thread.start()
        outcomes = {"leaked": 0, "refused": 0, "clean": 0}
        try:
            for _ in range(150):
                try:
                    request = self.engine._plan_request(parsed)
                except (WorkspaceError, EngineError):
                    outcomes["refused"] += 1
                    continue
                if any(SECRET_MARKER in text for text in request.files.values()):
                    outcomes["leaked"] += 1
                else:
                    outcomes["clean"] += 1
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(outcomes["leaked"], 0, outcomes)
        self.assertGreater(outcomes["refused"] + outcomes["clean"], 0)

    def test_source_changed_after_create_is_refused_before_any_provider_call(self):
        mission_id = self.engine.create(real_provider_spec(self.source))
        (self.source / "calc" / "stats.py").write_text("# altered after the mission was created\n")
        with self.assertRaises(EngineError):
            self.engine.plan(mission_id, allow_real_model=True)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.engine.store.state(mission_id), MissionState.FAILED)


class SourceMatchesApprovalTests(HardeningCase):
    def approved(self):
        mission_id = self.engine.create(real_provider_spec(self.source))
        plan_sha = self.engine.plan(mission_id, allow_real_model=True)
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        return mission_id

    def test_source_changed_between_approval_and_run_never_executes(self):
        mission_id = self.approved()
        (self.source / "calc" / "stats.py").write_text("# changed after approval\n")
        state = self.engine.run(mission_id)
        self.assertEqual(state, MissionState.FAILED)
        self.assertEqual(self.sandbox.runs, [], "unapproved code reached the sandbox")
        self.assertIn("source_changed", self.engine.store.mission(mission_id)["failure_reason"])

    def test_symlink_added_between_approval_and_run_never_executes(self):
        mission_id = self.approved()
        os.symlink("/etc/passwd", self.source / "calc" / "leak.py")
        self.assertEqual(self.engine.run(mission_id), MissionState.FAILED)
        self.assertEqual(self.sandbox.runs, [])

    def test_copies_are_built_from_the_verified_bytes(self):
        digest = tree_sha256(snapshot(self.source))
        base = Workspace.create(self.source, self.tmp / "a", "base", expect_tree_sha256=digest)
        work = Workspace.create(self.source, self.tmp / "a", "work", expect_tree_sha256=digest)
        self.assertEqual(tree_sha256(snapshot(base.root)), digest)
        self.assertEqual(tree_sha256(snapshot(work.root)), digest)
        with self.assertRaises(WorkspaceError) as caught:
            Workspace.create(self.source, self.tmp / "b", "work", expect_tree_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertFalse((self.tmp / "b" / "work").exists(), "a refused copy must leave nothing behind")


if __name__ == "__main__":
    unittest.main()
