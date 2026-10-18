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
        self.assertEqual(self.bypasses(job), [])

    @staticmethod
    def bypasses(job):
        """Ways a gate step could report success without enforcing (PR21-12)."""
        found = []
        if re.search(r"(?m)^\s*(?:-\s*)?continue-on-error:", job):
            found.append("continue-on-error")
        if re.search(r"(?m)^    if:", job):
            found.append("job-level if")
        if re.search(r"(?m)^\s*-?\s*if:", job):
            found.append("step-level if")
        if re.search(r"\|\|\s*(true|:|exit\s+0)\b", job):
            found.append("|| true")
        if re.search(r"(?m)^\s*set\s+\+e\b", job):
            found.append("set +e")
        return found

    def test_bypass_detector_rejects_weakened_jobs(self):
        base = "  open-core-boundary:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python scripts/ip_inventory.py --check\n"
        for weakened in (
            base.replace("--check\n", "--check || true\n"),
            base.replace("    steps:", "    if: ${{ false }}\n    steps:"),
            base.replace("      - run:", "      - continue-on-error: true\n        run:"),
            base.replace("      - run:", "      - if: false\n        run:"),
            base.replace("      - run: python", "      - run: |\n          set +e\n          python"),
        ):
            with self.subTest(job=weakened):
                self.assertTrue(self.bypasses(self.boundary_job(weakened)))
        self.assertEqual(self.bypasses(self.boundary_job(base)), [])

    def test_job_parser_does_not_count_commands_from_other_jobs(self):
        fake = "jobs:\n  unit-tests:\n    run: python scripts/ip_inventory.py --check\n"
        self.assertEqual(self.boundary_job(fake), "")


if __name__ == "__main__":
    unittest.main()
