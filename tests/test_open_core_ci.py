"""Keep the D1 public-boundary guard wired into the existing CI workflow."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/tests.yml"


class WorkflowContractTests(unittest.TestCase):
    @staticmethod
    def boundary_job(contents):
        hit = re.search(r"(?m)^  open-core-boundary:\s*$", contents)
        if hit is None:
            return ""
        next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\s*$", contents[hit.end():])
        end = hit.end() + next_job.start() if next_job else len(contents)
        return contents[hit.start():end]

    def test_workflow_includes_d1_boundary_gates(self):
        contents = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("unit-tests:", contents, "existing mainline job must remain")
        self.assertIn("fabric-mcp:", contents, "existing MCP suite must remain")
        self.assertIn("compat-openai-agents:", contents, "existing agents suite must remain")
        self.assertIn("benchmark-reproducibility:", contents)
        self.assertIn("can-replay-cantools:", contents)
        job = self.boundary_job(contents)
        self.assertTrue(job, "missing open-core-boundary job")
        self.assertIn("runs-on: ubuntu-latest", job)
        self.assertRegex(job, r"(?m)^    permissions:\s*\n      contents: read\s*$")
        self.assertIn("actions/checkout@v4", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("actions/setup-python@v5", job)
        self.assertRegex(job, r'python-version: ["\']3\.12["\']')
        for command in (
            "python scripts/ip_inventory.py --check",
            "python scripts/check_open_core_boundary.py --check",
            "python scripts/check_public_artifacts.py --wheel",
            "python -m pip wheel --no-deps",
            "python -m unittest discover -s tests -p 'test_evidence_proposal.py' -v",
        ):
            with self.subTest(command=command):
                self.assertIn(command, job)
        self.assertNotIn("contents: write", job)
        self.assertNotIn("secrets:", job)

    def test_job_parser_does_not_count_commands_from_other_jobs(self):
        fake = "jobs:\n  unit-tests:\n    run: python scripts/ip_inventory.py --check\n"
        self.assertEqual(self.boundary_job(fake), "")


if __name__ == "__main__":
    unittest.main()
