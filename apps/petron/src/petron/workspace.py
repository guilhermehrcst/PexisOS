"""Disposable workspace: a fresh copy of the mission's source tree, confined path access,
tree hashing and a unified diff. A workspace is *not* a sandbox; code never runs here on the
host. It is only the directory a sandbox mounts read-only.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import os
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


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC


def _read_all(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise WorkspaceError("tree_limit")
        chunks.append(chunk)


def _open_checked(name: str, dir_fd: int, flags: int, expected: os.stat_result, want) -> int:
    """Open ``name`` below ``dir_fd`` without following symlinks, then prove the opened object is
    the one that was classified: same device and inode, and still the expected type. Closes the
    window between "look at the entry" and "open it" that path-string checks leave open."""
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise WorkspaceError("symlink_in_tree") from None
        raise WorkspaceError("tree_changed_during_read") from None
    info = os.fstat(fd)
    if not want(info.st_mode) or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise WorkspaceError("tree_changed_during_read")
    return fd


def read_tree(root: Path, *, max_files: int = MAX_FILES, max_file_bytes: int = MAX_FILE_BYTES,
              max_tree_bytes: int = MAX_TREE_BYTES) -> dict[str, bytes]:
    """{relative posix path: bytes} of every regular file, read through directory file
    descriptors with ``O_NOFOLLOW``. Symlinks, special files and anything that changes identity
    while it is being read are refused (fail closed). Callers hash and use *these* bytes, so what
    was validated is exactly what is used."""
    result: dict[str, bytes] = {}
    total = 0

    def walk(dir_fd: int, prefix: str) -> None:
        nonlocal total
        for name in sorted(os.listdir(dir_fd)):
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                raise WorkspaceError("tree_changed_during_read") from None
            rel = f"{prefix}{name}"
            if stat.S_ISLNK(info.st_mode):
                raise WorkspaceError("symlink_in_tree")
            if stat.S_ISDIR(info.st_mode):
                if name in _SKIP_DIRS:
                    continue
                child = _open_checked(name, dir_fd, _DIR_FLAGS, info, stat.S_ISDIR)
                try:
                    walk(child, rel + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                fd = _open_checked(name, dir_fd, _FILE_FLAGS, info, stat.S_ISREG)
                try:
                    size = os.fstat(fd).st_size
                    if len(result) >= max_files or size > max_file_bytes or total + size > max_tree_bytes:
                        raise WorkspaceError("tree_limit")
                    data = _read_all(fd, max_file_bytes)
                finally:
                    os.close(fd)
                total += len(data)
                if total > max_tree_bytes:
                    raise WorkspaceError("tree_limit")
                result[rel] = data
            else:
                raise WorkspaceError("non_regular_file_in_tree")

    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except OSError as error:
        raise WorkspaceError("symlink_in_tree" if error.errno == errno.ELOOP else "source_unreadable") from None
    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    return result


def hash_files(contents: dict[str, bytes]) -> dict[str, str]:
    return {path: hashlib.sha256(data).hexdigest() for path, data in contents.items()}


def snapshot(root: Path) -> dict[str, str]:
    """{relative posix path: sha256} of every regular file. Rejects symlinks and special files."""
    return hash_files(read_tree(Path(root)))


def tree_sha256(files: dict[str, str]) -> str:
    material = "\n".join(f"{path}\0{digest}" for path, digest in sorted(files.items()))
    return hashlib.sha256(material.encode()).hexdigest()


def _read_regular(path: Path, limit: int | None = None) -> bytes:
    """Read one regular file by path without following a symlink at the last component."""
    try:
        fd = os.open(path, _FILE_FLAGS)
    except OSError as error:
        raise WorkspaceError("symlink_path" if error.errno == errno.ELOOP else "not_a_file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("not_a_file")
        if limit is not None and info.st_size > limit:
            raise WorkspaceError("file_too_large")
        return _read_all(fd, limit if limit is not None else MAX_FILE_BYTES)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def create(cls, source: Path, parent: Path, name: str, *, expect_tree_sha256: str | None = None) -> "Workspace":
        """Copy ``source`` into ``parent/name`` from the exact bytes that were hashed. With
        ``expect_tree_sha256`` the copy is refused (and nothing is written) unless that tree is
        what the source holds right now."""
        source = Path(source).resolve(strict=True)
        contents = read_tree(source)   # validates the tree and holds the bytes we will copy
        if expect_tree_sha256 is not None and tree_sha256(hash_files(contents)) != expect_tree_sha256:
            raise WorkspaceError("source_changed")
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        root = parent / name
        if root.exists():
            raise WorkspaceError("workspace_exists")
        # Readable by the sandbox's unprivileged uid; never writable by it (mounted read-only).
        os.mkdir(root, 0o755)
        os.chmod(root, 0o755)
        for rel, data in contents.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(target.parent, 0o755)
            _write_new(target, data)
            os.chmod(target, 0o644)
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
        return _read_regular(self.resolve(rel), limit).decode("utf-8", errors="replace")

    def replace_once(self, rel: str, old: str, new: str) -> None:
        path = self.resolve(rel)
        try:
            text = _read_regular(path).decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("not_utf8") from None
        count = text.count(old)
        if count != 1:
            raise WorkspaceError("old_text_not_unique" if count else "old_text_not_found")
        updated = text.replace(old, new, 1).encode()
        if len(updated) > MAX_FILE_BYTES:
            raise WorkspaceError("file_too_large")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | _O_CLOEXEC)
        except OSError:
            raise WorkspaceError("symlink_path") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceError("not_a_file")
            view = memoryview(updated)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.chmod(path, 0o644)


def _lines(root: Path, rel: str) -> list[str]:
    try:
        return _read_regular(Path(root) / rel).decode("utf-8", errors="replace").splitlines(keepends=True)
    except WorkspaceError:
        return []   # absent in this tree (added or removed file)


def unified_diff(before_root: Path, after_root: Path, paths: list[str]) -> str:
    chunks = []
    for rel in sorted(paths):
        chunks.extend(difflib.unified_diff(_lines(before_root, rel), _lines(after_root, rel),
                                           fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return "".join(chunks)


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
