"""brain/live.js draft handling, checked in Node against stubs (no browser needed).

The scenarios live in ``tests/js/live_draft_harness.js``. Without Node the test is skipped, which the
unittest report shows: a skip is "not run", never "passed".
"""

import shutil
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "js" / "live_draft_harness.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed: live.js draft scenarios were NOT run")
class LiveDraftTests(unittest.TestCase):
    def test_every_draft_scenario_passes(self):
        result = subprocess.run([NODE, str(HARNESS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("17/17 scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
