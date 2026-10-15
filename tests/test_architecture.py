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
            "componentx": {""},  # package root only (__version__): no detector, no evaluation harness
            "canreplay": set(),  # v0.4 Lab A core: standard library only
            "lab_cli": {"", "canreplay"},
        }
        for module, permitted in allowed.items():
            self.assertTrue(imported_modules(module) <= permitted,
                            f"{module} imports {imported_modules(module) - permitted}")

    def test_dataset_preflight_is_isolated_from_detection_and_evaluation(self):
        """v0.3 Stage A: the Component X preflight must not reach, or be reached by, the
        v0.1 comparator, the v0.2 detector or the benchmark (only the CLI wires it in)."""
        for path in PACKAGE.glob("*.py"):
            if path.stem not in ("componentx", "cli"):
                self.assertNotIn("componentx", imported_modules(path.stem), path.name)

    def test_can_replay_lab_is_isolated(self):
        """v0.4 Lab A: no detector, preflight or evaluation module reaches the lab (only the CLI wires
        it in); python-can is never imported by the package; cantools only inside canreplay."""
        for path in PACKAGE.glob("*.py"):
            if path.stem not in ("canreplay", "lab_cli", "cli"):
                self.assertFalse(imported_modules(path.stem) & {"canreplay", "lab_cli"}, path.name)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for name in names:
                    top = name.split(".")[0]
                    self.assertNotEqual(top, "can", f"{path.name} imports python-can")
                    if top == "cantools":
                        self.assertEqual(path.stem, "canreplay", f"{path.name} imports cantools")
        import subprocess
        import sys
        probe = ("import sys, pexislink.cli, pexislink.canreplay, pexislink.lab_cli; "
                 "print(sorted(m for m in ('can', 'cantools') if m in sys.modules))")
        result = subprocess.run([sys.executable, "-c", probe], env={"PYTHONPATH": str(PACKAGE.parent)},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr)  # importing the lab loads no decoder

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
            import tempfile

            from canreplay_fixtures import stub_factory
            from canreplay_fixtures import write_inputs as write_can_inputs
            from componentx_fixtures import write_dataset  # synthetic files: every reading path runs

            from pexislink import lab_cli
            with tempfile.TemporaryDirectory() as tmp:
                data = write_dataset(Path(tmp) / "data")
                self.assertEqual(main(["dataset", "preflight", "--source", "component-x-v3", "--input-dir",
                                       str(data), "--out", str(Path(tmp) / "m.json")]), 0)
                dbc, frames = write_can_inputs(Path(tmp) / "can")
                self.assertEqual(lab_cli.main(["can-replay", "--dbc", str(dbc), "--frames", str(frames), "--out",
                                               str(Path(tmp) / "r.jsonl"), "--summary", str(Path(tmp) / "s.json")],
                                              decoder_factory=stub_factory), 0)

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

    def test_no_process_creation_through_os(self):
        spawning = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
                    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"}
        for path in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.assertNotIn(node.attr, spawning, f"{path.name} uses os.{node.attr}")


if __name__ == "__main__":
    unittest.main()
