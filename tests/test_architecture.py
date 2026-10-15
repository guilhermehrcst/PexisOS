"""Structural guarantees enforced on the source code itself.

Label isolation is a scientific requirement: detection code must not be able to
read ground truth, even by accident. The check parses imports with ``ast``.
"""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pexislink"
DETECTION = ("ingest", "config", "quality", "reference", "detect", "core")
EVALUATION_ONLY = {"synth", "evaluate", "benchmark", "baselines"}


def imported_modules(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pexislink"):
            names.add((node.module or "").split(".")[1] if "." in node.module else "")
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[1] for alias in node.names
                         if alias.name.startswith("pexislink."))
    return names


class LayeringTests(unittest.TestCase):
    def test_detection_code_cannot_reach_ground_truth(self):
        for module in DETECTION:
            leaked = imported_modules(module) & EVALUATION_ONLY
            self.assertEqual(leaked, set(), f"{module} imports {leaked}")

    def test_layers_only_depend_downwards(self):
        allowed = {
            "ingest": set(),
            "config": {"ingest"},
            "quality": {"config", "ingest"},
            "reference": {"config", "ingest", "quality"},
            "detect": {"ingest", "quality", "reference"},
            "core": {"ingest"},
            "evaluate": {"synth"},
            "synth": {"ingest"},
            "baselines": {"core", "ingest"},
        }
        for module, permitted in allowed.items():
            self.assertTrue(imported_modules(module) <= permitted,
                            f"{module} imports {imported_modules(module) - permitted}")

    def test_no_network_or_subprocess_in_the_package(self):
        forbidden = {"socket", "urllib", "http", "subprocess", "requests", "ftplib", "smtplib", "ctypes"}
        for path in PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    used = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    used = {(node.module or "").split(".")[0]}
                else:
                    continue
                self.assertEqual(used & forbidden, set(), f"{path.name} imports {used & forbidden}")


if __name__ == "__main__":
    unittest.main()
