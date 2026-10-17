"""Fail-closed tripwires for public PexisOS source and tracked paths.

Repository tooling only. This cannot detect computed imports, malware or historical
leaks and is not a legal or security certification.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Sequence

from ip_inventory import tracked_paths

PRIVATE_MODULES = frozenset({"pexisos_enterprise", "pexisos_cloud"})
PRIVATE_PATH_ROOTS = (
    "private/", "enterprise/", "cloud/",
    "src/pexisos_enterprise/", "src/pexisos_cloud/",
)
_PRIVATE_REFERENCE = re.compile(
    r"(?<![a-zA-Z0-9_])pexisos[-_](?:enterprise|cloud)(?![a-zA-Z0-9_])",
    re.IGNORECASE,
)


def _private_module(module: str) -> bool:
    return module.partition(".")[0] in PRIVATE_MODULES


def inspect_imports(source: str, filename: str) -> list[str]:
    """Identify private imports in syntactically valid public Python code.

    Findings use source location and module names, never source excerpts.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, TypeError):
        return [f"{filename}:syntax_error"]
    issues = []
    importlib_aliases = {"importlib"}
    direct_functions = {"__import__", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                if _private_module(alias.name):
                    issues.append(f"{filename}:{node.lineno}:private_import:{alias.name.partition('.')[0]}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _private_module(node.module):
                issues.append(f"{filename}:{node.lineno}:private_import:{node.module.partition('.')[0]}")
            if node.level == 0 and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        direct_functions.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        direct = isinstance(func, ast.Name) and func.id in direct_functions
        importlib_call = (isinstance(func, ast.Attribute) and func.attr == "import_module"
                          and isinstance(func.value, ast.Name) and func.value.id in importlib_aliases)
        if not (direct or importlib_call):
            continue
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str) and _private_module(target.value):
            issues.append(f"{filename}:{node.lineno}:private_dynamic_import:{target.value.partition('.')[0]}")
    return sorted(set(issues))


def inspect_tracked_paths(paths: Sequence[str]) -> list[str]:
    """Check public Git index for known private directory or secret-file paths."""
    findings = []
    for path in paths:
        path = path.replace("\\", "/")
        normalized = path.lower()
        basename = normalized.rsplit("/", 1)[-1]
        private_root = normalized.startswith(PRIVATE_PATH_ROOTS)
        secret_file = basename == ".env" or basename.startswith(".env.") or basename.endswith((".pem", ".key"))
        if private_root or secret_file:
            findings.append(f"private_tracked_path:{path}")
    return sorted(set(findings))


def _contains_private_reference(value: object) -> bool:
    if isinstance(value, str):
        return bool(_PRIVATE_REFERENCE.search(value))
    if isinstance(value, dict):
        return any(_contains_private_reference(key) or _contains_private_reference(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_reference(item) for item in value)
    return False


def inspect_packaging(pyproject: dict) -> list[str]:
    """Inspect dependency/entry-point/build configuration without copying secret values."""
    findings = []
    if not isinstance(pyproject, dict):
        return ["invalid_pyproject:expected_object"]
    project = pyproject.get("project", {})
    tool = pyproject.get("tool", {})
    if not isinstance(project, dict) or not isinstance(tool, dict):
        return ["invalid_pyproject:section_not_object"]
    for path, value in (
        ("build-system", pyproject.get("build-system", {})),
        ("project.dependencies", project.get("dependencies", [])),
        ("project.optional-dependencies", project.get("optional-dependencies", {})),
        ("project.scripts", project.get("scripts", {})),
        ("project.entry-points", project.get("entry-points", {})),
        ("tool.setuptools", tool.get("setuptools", {})),
    ):
        if _contains_private_reference(value):
            findings.append(f"private_packaging_reference:{path}")
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static public/private boundary guard for the PexisOS repository")
    parser.add_argument("--check", required=True, action="store_true")
    parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        paths = tracked_paths(root)
        if not paths:
            print("boundary_unavailable:empty_index", file=sys.stderr)
            return 2
        findings = inspect_tracked_paths(paths)
        for filename in paths:
            if filename.startswith("src/pexislink/") and filename.endswith(".py"):
                source = (root / filename).read_text(encoding="utf-8")
                findings.extend(inspect_imports(source, filename))
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        findings.extend(inspect_packaging(pyproject))
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired, tomllib.TOMLDecodeError) as exc:
        print(f"boundary_unavailable:{type(exc).__name__}", file=sys.stderr)
        return 2
    for finding in sorted(set(findings)):
        print(finding, file=sys.stderr)
    if findings:
        return 1
    print("open_core_boundary_clear: static imports, tracked paths and project packaging")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
