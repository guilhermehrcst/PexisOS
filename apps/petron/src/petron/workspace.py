"""Disposable workspace: a fresh copy of the mission's source tree, confined path access,
tree hashing and a unified diff. A workspace is *not* a sandbox; code never runs here on the
host. It is only the directory a sandbox mounts read-only.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
from pathlib import Path

MAX_FILES = 2000
MAX_FILE_BYTES = 256 * 1024
MAX_TREE_BYTES = 16 * 1024 * 1024
_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})


class WorkspaceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def snapshot(root: Path) -> dict[str, str]:
    """{relative posix path: sha256} of every regular file. Rejects symlinks and special files."""
    root = Path(root)
    result: dict[str, str] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for directory in dirnames:
            if os.path.islink(os.path.join(dirpath, directory)):
                raise WorkspaceError("symlink_in_tree")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError("non_regular_file_in_tree")
            total += info.st_size
            if len(result) >= MAX_FILES or info.st_size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                raise WorkspaceError("tree_limit")
            rel = Path(full).relative_to(root).as_posix()
            with open(full, "rb") as handle:
                result[rel] = hashlib.sha256(handle.read()).hexdigest()
    return result


def tree_sha256(files: dict[str, str]) -> str:
    material = "\n".join(f"{path}\0{digest}" for path, digest in sorted(files.items()))
    return hashlib.sha256(material.encode()).hexdigest()


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def create(cls, source: Path, parent: Path, name: str) -> "Workspace":
        source = Path(source).resolve(strict=True)
        snapshot(source)  # validates the source tree before copying
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        root = parent / name
        if root.exists():
            raise WorkspaceError("workspace_exists")
        shutil.copytree(source, root, symlinks=True,
                        ignore=shutil.ignore_patterns(*_SKIP_DIRS))
        # Readable by the sandbox's unprivileged uid; never writable by it (mounted read-only).
        for dirpath, dirnames, filenames in os.walk(root):
            os.chmod(dirpath, 0o755)
            for name_ in filenames:
                os.chmod(os.path.join(dirpath, name_), 0o644)
        return cls(root)

    def resolve(self, rel: str) -> Path:
        """Confine ``rel`` to the workspace: no absolute paths, no ``..``, no symlink hops."""
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
            raise WorkspaceError("invalid_path")
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts) or any(part in _SKIP_DIRS for part in parts):
            raise WorkspaceError("invalid_path")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("symlink_path")
        resolved = current.resolve()
        if resolved != self.root.resolve() and self.root.resolve() not in resolved.parents:
            raise WorkspaceError("path_escape")
        return current

    def list_files(self) -> list[str]:
        return sorted(snapshot(self.root))

    def read_text(self, rel: str, limit: int = 64 * 1024) -> str:
        path = self.resolve(rel)
        info = os.lstat(path) if path.exists() else None
        if info is None or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("not_a_file")
        if info.st_size > limit:
            raise WorkspaceError("file_too_large")
        return path.read_text(encoding="utf-8", errors="replace")

    def replace_once(self, rel: str, old: str, new: str) -> None:
        path = self.resolve(rel)
        if not path.is_file():
            raise WorkspaceError("not_a_file")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise WorkspaceError("old_text_not_unique" if count else "old_text_not_found")
        updated = text.replace(old, new, 1)
        if len(updated.encode()) > MAX_FILE_BYTES:
            raise WorkspaceError("file_too_large")
        path.write_text(updated, encoding="utf-8")
        os.chmod(path, 0o644)


def unified_diff(before_root: Path, after_root: Path, paths: list[str]) -> str:
    chunks = []
    for rel in sorted(paths):
        before = (Path(before_root) / rel)
        after = (Path(after_root) / rel)
        a = before.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if before.is_file() else []
        b = after.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if after.is_file() else []
        chunks.extend(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return "".join(chunks)


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
