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

    def test_no_process_is_spawned_at_runtime(self):
        """Code review L10: platform.platform() used to spawn `uname -p` during a
        benchmark. Static import checks cannot see that, so spy on process creation."""
        import io
        import subprocess
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock

        from pexislink.benchmark import Profile, run_benchmark
        from pexislink.cli import main

        examples = PACKAGE.parents[1] / "examples"
        with mock.patch.object(subprocess.Popen, "__init__", side_effect=AssertionError("spawned a process")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            run_benchmark(Profile("tiny", "dev", (101,), 4, 13), ["normal_operation"])
            self.assertEqual(main(["detect", "--reference", str(examples / "baseline.csv"),
                                   "--observations", str(examples / "observations.csv")]), 0)

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
