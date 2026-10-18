"""Fail-closed tripwires for public PexisOS source and tracked paths.

Repository tooling only. This cannot detect computed imports, malware or historical
leaks and is not a legal or security certification.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Iterable, Sequence

from open_core_names import is_private_path_segment, mentions_private_name, safe_text

PRIVATE_PATH_ROOTS = ("private/", "enterprise/", "cloud/")
_GITLINK_MODE = "160000"
_SYMLINK_MODE = "120000"
_MAX_DYNAMIC_FILE_BYTES = 1024 * 1024


def _private_module(module: str) -> bool:
    return mentions_private_name(module)


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _import_target(call: ast.Call) -> str | None:
    """The module named by ``import_module``/``__import__``, positional or ``name=``.

    A relative target (``".x"``) is resolved against a literal ``package`` argument,
    so ``import_module(".x", package="pexisos_cloud")`` is seen as private.
    """
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    target = _literal(call.args[0] if call.args else keywords.get("name"))
    if target is None or not target.startswith("."):
        return target
    package = _literal(call.args[1] if len(call.args) > 1 else keywords.get("package"))
    return f"{package}{target}" if package else target


def inspect_imports(source: str, filename: str) -> list[str]:
    """Identify private imports in syntactically valid public Python code.

    Findings use source location and module names, never source excerpts.
    """
    shown = safe_text(filename)
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError, TypeError):
        return [f"{shown}:syntax_error"]
    issues = []
    import_modules = {"importlib", "builtins"}
    direct_functions = {"__import__", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("importlib", "builtins"):
                    import_modules.add(alias.asname or alias.name)
                if _private_module(alias.name):
                    issues.append(f"{shown}:{node.lineno}:private_import:{safe_text(alias.name.partition('.')[0])}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _private_module(node.module):
                issues.append(f"{shown}:{node.lineno}:private_import:{safe_text(node.module.partition('.')[0])}")
            if node.level == 0 and node.module in ("importlib", "builtins"):
                for alias in node.names:
                    if alias.name in ("import_module", "__import__"):
                        direct_functions.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        direct = isinstance(func, ast.Name) and func.id in direct_functions
        module_call = (isinstance(func, ast.Attribute) and func.attr in ("import_module", "__import__")
                       and isinstance(func.value, ast.Name) and func.value.id in import_modules)
        if not (direct or module_call):
            continue
        target = _import_target(node)
        if target is not None and _private_module(target.lstrip(".")):
            root_name = target.lstrip(".").partition(".")[0]
            issues.append(f"{shown}:{node.lineno}:private_dynamic_import:{safe_text(root_name)}")
    return sorted(set(issues))


def inspect_tracked_paths(paths: Sequence[str]) -> list[str]:
    """Check public Git index paths for private roots, private module names and secret files.

    A private name is rejected in any segment and in any form: package directory,
    single-file module (``.py``, ``.pyd``, ``.so``, ``.pth``) or versioned metadata.
    """
    findings = []
    for path in paths:
        path = path.replace("\\", "/")
        normalized = path.lower()
        segments = normalized.split("/")
        basename = segments[-1]
        private_root = normalized.startswith(PRIVATE_PATH_ROOTS)
        private_name = any(is_private_path_segment(segment) for segment in segments)
        secret_file = basename == ".env" or basename.startswith(".env.") or basename.endswith((".pem", ".key"))
        if private_root or private_name or secret_file:
            findings.append(f"private_tracked_path:{safe_text(path)}")
    return sorted(set(findings))


def inspect_tracked_entries(entries: Iterable[tuple[str, str]]) -> list[str]:
    """Check (mode, path) index entries: names as above, plus gitlinks and symlinks.

    A gitlink (submodule) brings code that is neither inventoried nor scanned, and a
    tracked symlink can make a guard read a file outside the repository; both are refused.
    """
    entries = list(entries)
    findings = inspect_tracked_paths([path for _, path in entries])
    for mode, path in entries:
        if mode == _GITLINK_MODE:
            findings.append(f"gitlink_tracked_path:{safe_text(path)}")
        elif mode == _SYMLINK_MODE:
            findings.append(f"symlink_tracked_path:{safe_text(path)}")
    return sorted(set(findings))


def tracked_entries(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """(mode, path) for every Git index entry, in deterministic order."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-s", "-z"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    entries = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        entries.append((meta.split(b" ", 1)[0].decode("ascii"), path.decode("utf-8")))
    return tuple(sorted(entries, key=lambda entry: entry[1]))


def _contains_private_reference(value: object) -> bool:
    if isinstance(value, str):
        return mentions_private_name(value)
    if isinstance(value, dict):
        return any(_contains_private_reference(key) or _contains_private_reference(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_reference(item) for item in value)
    return False


def _read_repository_file(root: Path, reference: object) -> str | None:
    """Text of a repository-relative regular file, or None if missing, escaping or a symlink."""
    if not isinstance(reference, str) or not reference:
        return None
    base = root.resolve()
    candidate = root / reference
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        resolved = candidate.resolve(strict=True)
        if base not in resolved.parents:
            return None
        with resolved.open("rb") as handle:
            raw = handle.read(_MAX_DYNAMIC_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_DYNAMIC_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _dynamic_file_references(dynamic: dict) -> list[tuple[str, object]]:
    """(field label, file reference) for setuptools dynamic metadata read from files."""
    references: list[tuple[str, object]] = []

    def add(label: str, spec: object) -> None:
        if isinstance(spec, dict) and "file" in spec:
            files = spec["file"]
            for reference in (files if isinstance(files, list) else [files]):
                references.append((label, reference))

    for key in ("dependencies", "entry-points", "scripts", "gui-scripts"):
        add(f"tool.setuptools.dynamic.{key}.file", dynamic.get(key))
    optional = dynamic.get("optional-dependencies")
    if isinstance(optional, dict):
        for spec in optional.values():
            add("tool.setuptools.dynamic.optional-dependencies.file", spec)
    return references


def inspect_packaging(pyproject: dict, root: Path | None = None) -> list[str]:
    """Inspect dependency/entry-point/build configuration without copying secret values.

    Names are compared after PEP 503 normalization. Files referenced by setuptools
    dynamic metadata are read from ``root``; when they cannot be read safely (missing,
    outside the repository, a symlink, too large, or no ``root``) the check fails closed.
    """
    findings = []
    if not isinstance(pyproject, dict):
        return ["invalid_pyproject:expected_object"]
    project = pyproject.get("project", {})
    tool = pyproject.get("tool", {})
    groups = pyproject.get("dependency-groups", {})
    if not isinstance(project, dict) or not isinstance(tool, dict) or not isinstance(groups, dict):
        return ["invalid_pyproject:section_not_object"]
    fields = [
        ("build-system", pyproject.get("build-system", {})),
        ("project.dependencies", project.get("dependencies", [])),
        ("project.optional-dependencies", project.get("optional-dependencies", {})),
        ("project.scripts", project.get("scripts", {})),
        ("project.gui-scripts", project.get("gui-scripts", {})),
        ("project.entry-points", project.get("entry-points", {})),
        ("dependency-groups", groups),
    ]
    fields.extend((f"tool.{safe_text(str(name))}", value) for name, value in tool.items())
    for path, value in fields:
        if _contains_private_reference(value):
            findings.append(f"private_packaging_reference:{path}")
    setuptools = tool.get("setuptools", {})
    dynamic = setuptools.get("dynamic", {}) if isinstance(setuptools, dict) else {}
    for label, reference in _dynamic_file_references(dynamic if isinstance(dynamic, dict) else {}):
        text = None if root is None else _read_repository_file(root, reference)
        if text is None:
            findings.append(f"unreadable_dynamic_dependency_file:{label}")
            continue
        lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
        if any(line and mentions_private_name(line) for line in lines):
            findings.append(f"private_packaging_reference:{label}")
    return sorted(set(findings))


def check_repository(root: Path) -> list[str]:
    """All boundary findings for the Git index rooted at ``root``.

    Raises OSError, UnicodeError, ValueError (including TOMLDecodeError) or subprocess
    errors when the repository cannot be read; callers must treat that as a failure.
    """
    entries = tracked_entries(root)
    if not entries:
        raise ValueError("empty_index")
    findings = inspect_tracked_entries(entries)
    for mode, filename in entries:
        # Every tracked Python file is public source: packages beside src/pexislink/,
        # scripts, examples and tests can all reach a private module. Gitlinks and
        # symlinks are refused above and never read.
        if filename.endswith(".py") and mode not in (_GITLINK_MODE, _SYMLINK_MODE):
            source = (root / filename).read_text(encoding="utf-8")
            findings.extend(inspect_imports(source, filename))
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.is_symlink():
        raise ValueError("pyproject_symlink")
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    findings.extend(inspect_packaging(pyproject, root))
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static public/private boundary guard for the PexisOS repository")
    parser.add_argument("--check", required=True, action="store_true")
    parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        findings = check_repository(root)
    except ValueError as exc:
        reason = str(exc) if str(exc) in ("empty_index", "pyproject_symlink") else type(exc).__name__
        print(f"boundary_unavailable:{reason}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"boundary_unavailable:{type(exc).__name__}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        return 1
    print("open_core_boundary_clear: tracked paths and modes, static imports in tracked Python, packaging")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
