"""Validate wheel and source-distribution archive names without extracting files.

Defense-in-depth path checks only. This does not search bodies or Git history
and cannot prove the absence of confidential information.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import stat
import sys
import tarfile
import zipfile

_PRIVATE_DIRS = frozenset({"private", "enterprise", "cloud", "pexisos_enterprise", "pexisos_cloud"})
_MAX_ENTRIES = 20_000
_MAX_UNCOMPRESSED = 128 * 1024 * 1024


def _normalize(name: str) -> tuple[str | None, str | None]:
    text = name.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None, "absolute_archive_path"
    segments = text.rstrip("/").split("/")
    if not text or any(segment in ("", ".", "..") for segment in segments):
        return None, "path_traversal"
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None, "invalid_archive_path"
    return "/".join(segments), None


def _private_path(name: str) -> bool:
    segments = name.lower().split("/")
    basename = segments[-1]
    return (
        any(part in _PRIVATE_DIRS for part in segments[:-1])
        or basename == ".env"
        or basename.startswith(".env.")
        or basename.endswith((".pem", ".key"))
    )


def inspect_archive(archive_path: Path) -> list[str]:
    """Stable archive findings; no extraction and no file contents in diagnostics."""
    path = Path(archive_path)
    low = path.name.lower()
    is_zip = low.endswith((".whl", ".zip"))
    is_tar = low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))
    if not (is_zip or is_tar):
        return ["unsupported_archive_extension"]
    issues: list[str] = []
    try:
        if is_zip:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries or all(entry.is_dir() for entry in entries):
                    return ["empty_archive"]
                if len(entries) > _MAX_ENTRIES:
                    return ["archive_member_count_limit"]
                total = sum(entry.file_size for entry in entries)
                if total > _MAX_UNCOMPRESSED:
                    return ["archive_uncompressed_size_limit"]
                raw = []
                for entry in entries:
                    mode = entry.external_attr >> 16
                    member_type = stat.S_IFMT(mode)
                    if entry.flag_bits & 1:
                        issues.append(f"encrypted_archive_member:{entry.filename}")
                    if member_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                        issues.append(f"unsupported_member_type:{entry.filename}")
                    raw.append(entry.filename)
                if not issues and archive.testzip() is not None:
                    issues.append("invalid_archive_payload")
        else:
            with tarfile.open(path, mode="r:*") as archive:
                entries = archive.getmembers()
                if not entries or all(entry.isdir() for entry in entries):
                    return ["empty_archive"]
                if len(entries) > _MAX_ENTRIES:
                    return ["archive_member_count_limit"]
                if sum(member.size for member in entries) > _MAX_UNCOMPRESSED:
                    return ["archive_uncompressed_size_limit"]
                raw = []
                for entry in entries:
                    if not (entry.isfile() or entry.isdir()):
                        issues.append(f"unsupported_member_type:{entry.name}")
                    raw.append(entry.name)
    except (OSError, EOFError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError, NotImplementedError):
        return ["unreadable_archive"]
    normalized = []
    for name in raw:
        candidate, error = _normalize(name)
        if error:
            issues.append(f"{error}:{name}")
        elif candidate is not None:
            normalized.append(candidate)
    if is_tar and normalized:
        suffix = next(ext for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar") if low.endswith(ext))
        expected_root = path.name[:-len(suffix)]
        first_parts = [entry.split("/", 1) for entry in normalized]
        if all(parts[0] == expected_root for parts in first_parts) and any(len(parts) == 2 for parts in first_parts):
            # Strip only the archive's known sdist root, never an arbitrary single private directory.
            normalized = [entry.split("/", 1)[1] for entry in normalized if "/" in entry]
    if not normalized:
        issues.append("no_valid_archive_members")
    for name in normalized:
        if _private_path(name):
            issues.append(f"private_archive_path:{name}")
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject private and malformed paths in public PexisOS distributions")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path)
    args = parser.parse_args(argv)
    findings = []
    for name, path in (("wheel", args.wheel), ("sdist", args.sdist)):
        if path is not None:
            findings.extend(f"{name}:{finding}" for finding in inspect_archive(path))
    if findings:
        for line in sorted(findings):
            print(line, file=sys.stderr)
        return 1
    print("public_artifacts_clear: inspected supplied archive(s), not the full release history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
