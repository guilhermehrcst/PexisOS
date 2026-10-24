"""Fail-closed regression tests for Git provenance in PETRON Brain E2E reports."""
from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parent / "e2e" / "brain_provenance.py"
spec = importlib.util.spec_from_file_location("brain_e2e_provenance", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrainProvenanceTests(unittest.TestCase):
    SHA = "a" * 40

    def run_fake(self, results):
        calls = []
        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "git")
            self.assertTrue(kwargs.get("check"), "failing Git commands must raise")
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["cwd"], MODULE_PATH.parent)
            calls.append(tuple(args[1:]))
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")
        with patch.object(module.subprocess, "run", side_effect=fake_run):
            value = module.provenance(MODULE_PATH.parent)
        return value, calls

    def test_clean_tree_has_grounded_sha_and_false_dirty(self):
        value, calls = self.run_fake(iter([self.SHA + "\n", ""]))
        self.assertEqual(value["head"], self.SHA)
        self.assertIs(value["dirty"], False)
        self.assertEqual(calls, [("rev-parse", "HEAD"), ("status", "--porcelain", "--", ".")])

    def test_modified_tree_is_marked_dirty(self):
        value, _ = self.run_fake(iter([self.SHA + "\n", " M brain_e2e.py\n"]))
        self.assertIs(value["dirty"], True)

    def test_git_head_failure_never_claims_clean(self):
        failure = subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])
        value, _ = self.run_fake(iter([failure]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])

    def test_git_status_failure_never_claims_clean(self):
        failure = subprocess.CalledProcessError(128, ["git", "status", "--porcelain"])
        value, _ = self.run_fake(iter([self.SHA + "\n", failure]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])

    def test_git_unavailable_is_unknown_not_clean(self):
        value, _ = self.run_fake(iter([FileNotFoundError("git")]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])

    def test_git_timeout_is_unknown_not_clean(self):
        value, _ = self.run_fake(iter([subprocess.TimeoutExpired(["git"], 30)]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])

    def test_empty_head_is_unknown_not_clean(self):
        value, _ = self.run_fake(iter(["", ""]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])

    def test_non_hex_head_is_unknown_not_clean(self):
        value, _ = self.run_fake(iter(["not-a-commit", ""]))
        self.assertIsNone(value["head"])
        self.assertIsNone(value["dirty"])


if __name__ == "__main__":
    unittest.main()
