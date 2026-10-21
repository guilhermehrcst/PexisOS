"""PETRON and the PexisOS core stay operationally independent inside one repository.

* PETRON never imports ``pexislink``: it reaches the Agent Fabric only by launching its
  read-only MCP stdio server as a separate process.
* The PexisOS core never imports ``petron`` and does not depend on it.

Static checks (AST and packaging metadata); they run wherever the files are present.
"""

import ast
import tomllib
import unittest
from pathlib import Path

PETRON_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PETRON_ROOT.parents[1]
CORE_SRC = REPO_ROOT / "src" / "pexislink"
IN_MONOREPO = CORE_SRC.is_dir() and (REPO_ROOT / "pyproject.toml").is_file()


def imported_roots(tree_root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(tree_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                    "import_module", "__import__", "run_module", "find_spec"):
                names = [arg.value for arg in node.args[:1] if isinstance(arg, ast.Constant)
                         and isinstance(arg.value, str)]
            for name in names:
                found.setdefault(name.split(".")[0], set()).add(str(path.relative_to(tree_root)))
    return found


class PetronDoesNotImportCore(unittest.TestCase):
    def test_no_pexislink_import_in_petron(self):
        # The connector names 'pexislink' only inside the launcher string run by the child process.
        self.assertNotIn("pexislink", imported_roots(PETRON_ROOT / "src"))

    def test_petron_has_no_mandatory_dependencies(self):
        project = tomllib.loads((PETRON_ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["dependencies"], [])
        self.assertNotIn("pexislink", str(project.get("optional-dependencies", {})).lower())


@unittest.skipUnless(IN_MONOREPO, "not inside the PexisOS monorepo")
class CoreDoesNotDependOnPetron(unittest.TestCase):
    def test_core_never_imports_petron(self):
        self.assertNotIn("petron", imported_roots(CORE_SRC))

    def test_core_package_metadata_does_not_reference_petron(self):
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        project = pyproject["project"]
        self.assertEqual(project["dependencies"], [])
        self.assertNotIn("petron", str(project.get("optional-dependencies", {})).lower())
        self.assertEqual(pyproject["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])


if __name__ == "__main__":
    unittest.main()
