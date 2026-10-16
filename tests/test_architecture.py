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

            from componentx_fixtures import write_dataset  # synthetic files: every reading path runs
            with tempfile.TemporaryDirectory() as tmp:
                data = write_dataset(Path(tmp) / "data")
                self.assertEqual(main(["dataset", "preflight", "--source", "component-x-v3", "--input-dir",
                                       str(data), "--out", str(Path(tmp) / "m.json")]), 0)
            self.assertEqual(main(["fabric", "demo"]), 0)

    def test_no_network_or_subprocess_in_the_package(self):
        forbidden = {"socket", "urllib", "http", "subprocess", "requests", "ftplib", "smtplib", "ctypes"}
        for path in PACKAGE.rglob("*.py"):  # includes the fabric subpackage
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
        for path in PACKAGE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.assertNotIn(node.attr, spawning, f"{path.name} uses os.{node.attr}")



FABRIC = PACKAGE / "fabric"
OPTIONAL_TRANSPORT = {"mcp", "mcp_types", "anyio", "pydantic", "starlette", "uvicorn", "httpx", "httpx2"}


def _absolute_imports(path: Path) -> list[tuple[str, int, str]]:
    """(module, relative level, name) for every import statement in ``path``."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.extend((node.module or "", node.level, alias.name) for alias in node.names)
    return found


class AgentFabricIsolationTests(unittest.TestCase):
    """Agent Fabric v0.1 is an experimental, separate subpackage: it must not reach the
    v0.1 comparator, the v0.2 detector, the v0.3 preflight, the benchmark or ground
    truth, and no existing module may depend on it (only the CLI dispatches to it)."""

    def test_fabric_does_not_import_the_rest_of_the_package(self):
        for path in FABRIC.rglob("*.py"):
            depth = len(path.relative_to(FABRIC).parts)  # 1 = fabric/x.py, 2 = fabric/adapters/x.py
            for module, level, _ in _absolute_imports(path):
                self.assertLessEqual(level, depth, f"{path.name} reaches outside the fabric package")
                if level == 0 and module.startswith("pexislink"):
                    self.assertTrue(module.startswith("pexislink.fabric"), f"{path.name} imports {module}")

    def test_no_existing_module_imports_the_fabric(self):
        for path in PACKAGE.glob("*.py"):
            if path.stem == "cli":
                continue
            self.assertNotIn("fabric", imported_modules(path.stem), path.name)

    def test_fabric_core_is_standard_library_only(self):
        import sys

        for path in FABRIC.rglob("*.py"):
            for module, level, _ in _absolute_imports(path):
                if level or not module:
                    continue
                top = module.split(".")[0]
                if top in OPTIONAL_TRANSPORT:
                    self.assertIn(path.name, ("mcp_server.py", "cli.py"), f"{path.name} imports {module}")
                else:
                    self.assertTrue(top in sys.stdlib_module_names or top == "pexislink", f"{path.name}: {module}")

    def test_mcp_sdk_is_imported_only_by_the_optional_transport(self):
        for path in FABRIC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level = {(n.module or "").split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom)} | {
                a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
            if path.name != "mcp_server.py":
                self.assertEqual(top_level & OPTIONAL_TRANSPORT, set(), f"{path.name} imports the SDK at load time")

    def test_importing_the_fabric_does_not_load_the_mcp_sdk(self):
        import subprocess
        import sys

        code = ("import sys; import pexislink.fabric, pexislink.fabric.cli, pexislink.fabric.demo; "
                "bad = [m for m in ('mcp', 'mcp_types', 'anyio', 'pydantic') if m in sys.modules]; "
                "print(','.join(bad), end=''); raise SystemExit(1 if bad else 0)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                env={"PYTHONPATH": str(PACKAGE.parent), "PATH": ""})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_no_request_facing_code_can_write_policy(self):
        """Grants and revocations are operator APIs: the Session and the MCP transport never call them."""
        for name in ("runtime.py", "mcp_server.py"):
            tree = ast.parse((FABRIC / name).read_text(encoding="utf-8"))
            calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            self.assertEqual(calls & {"add_grant", "revoke"}, set(), name)


if __name__ == "__main__":
    unittest.main()
