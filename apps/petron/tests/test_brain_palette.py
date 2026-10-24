"""How brain.js chooses its palette, run in Node against stubs (skipped, i.e. NOT run, without Node)."""

import shutil
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "js" / "brain_palette_harness.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed: brain.js palette scenarios were NOT run")
class BrainPaletteTests(unittest.TestCase):
    def test_every_palette_scenario_passes(self):
        result = subprocess.run([NODE, str(HARNESS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("10/10 palette scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
