"""Validate wheel and source-distribution archives without extracting files.

Defense-in-depth checks only: member names everywhere, plus the bodies of the small
packaging-metadata files that decide what an installer will fetch or run
(``METADATA``/``PKG-INFO`` requirement fields, ``entry_points.txt``, ``requires.txt``).
Other file bodies and Git history are not searched, so this cannot prove the absence
of confidential information.
"""

from __future__ import annotations

import argparse
from email.parser import HeaderParser
from pathlib import Path
import re
import stat
import sys
import tarfile
import zipfile

from open_core_names import is_private_path_segment, mentions_private_name, safe_text

_PRIVATE_DIRS = frozenset({"private", "enterprise", "cloud"})
_MAX_ENTRIES = 20_000
_MAX_UNCOMPRESSED = 128 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_HEADER_METADATA = frozenset({"metadata", "pkg-info"})
_LINE_METADATA = frozenset({"entry_points.txt", "requires.txt"})
_REQUIREMENT_HEADERS = ("Name", "Requires-Dist", "Requires", "Provides-Dist", "Obsoletes-Dist")


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
    """A full member path (sdist root included, never stripped) that names private code."""
    segments = name.lower().split("/")
    basename = segments[-1]
    return (
        any(part in _PRIVATE_DIRS for part in segments[:-1])
        or any(is_private_path_segment(part) for part in segments)
        or basename == ".env"
        or basename.startswith(".env.")
        or basename.endswith((".pem", ".key"))
    )


def _metadata_kind(name: str) -> str | None:
    basename = name.rsplit("/", 1)[-1].lower()
    if basename in _HEADER_METADATA:
        return "headers"
    if basename in _LINE_METADATA:
        return "lines"
    return None


def _metadata_mentions_private(kind: str, raw: bytes) -> bool:
    text = raw.decode("utf-8", errors="replace")
    if kind == "headers":
        headers = HeaderParser().parsestr(text, headersonly=True)
        return any(mentions_private_name(value)
                   for field in _REQUIREMENT_HEADERS for value in headers.get_all(field, []))
    for line in text.splitlines():
        content = line.split("#", 1)[0].strip()
        if content and mentions_private_name(content):
            return True
    return False


def _list_zip(path: Path, issues: list[str]) -> tuple[list[str], dict[str, bytes]] | list[str]:
    metadata: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or all(entry.is_dir() for entry in entries):
            return ["empty_archive"]
        if len(entries) > _MAX_ENTRIES:
            return ["archive_member_count_limit"]
        if sum(entry.file_size for entry in entries) > _MAX_UNCOMPRESSED:
            return ["archive_uncompressed_size_limit"]
        raw = []
        for entry in entries:
            member_type = stat.S_IFMT(entry.external_attr >> 16)
            encrypted = bool(entry.flag_bits & 1)
            if encrypted:
                issues.append(f"encrypted_archive_member:{safe_text(entry.filename)}")
            if member_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                issues.append(f"unsupported_member_type:{safe_text(entry.filename)}")
            raw.append(entry.filename)
            if not entry.is_dir() and not encrypted and _metadata_kind(entry.filename):
                if entry.file_size > _MAX_METADATA_BYTES:
                    issues.append(f"metadata_size_limit:{safe_text(entry.filename)}")
                else:
                    with archive.open(entry) as handle:
                        metadata[entry.filename] = handle.read(_MAX_METADATA_BYTES + 1)
        if not issues and archive.testzip() is not None:
            issues.append("invalid_archive_payload")
    return raw, metadata


def _list_tar(path: Path, issues: list[str]) -> tuple[list[str], dict[str, bytes]] | list[str]:
    metadata: dict[str, bytes] = {}
    raw: list[str] = []
    total = 0
    has_file = False
    with tarfile.open(path, mode="r:*") as archive:
        # Iterate header by header so the count and declared-size limits stop the scan
        # before a large or numerous member is decompressed, not after reading it all.
        for entry in archive:
            if len(raw) >= _MAX_ENTRIES:
                return ["archive_member_count_limit"]
            total += max(entry.size, 0)
            if total > _MAX_UNCOMPRESSED:
                return ["archive_uncompressed_size_limit"]
            if not (entry.isfile() or entry.isdir()):
                issues.append(f"unsupported_member_type:{safe_text(entry.name)}")
            raw.append(entry.name)
            has_file = has_file or not entry.isdir()
            if entry.isfile() and _metadata_kind(entry.name):
                if entry.size > _MAX_METADATA_BYTES:
                    issues.append(f"metadata_size_limit:{safe_text(entry.name)}")
                else:
                    handle = archive.extractfile(entry)
                    metadata[entry.name] = handle.read(_MAX_METADATA_BYTES + 1) if handle else b""
    if not has_file:
        return ["empty_archive"]
    return raw, metadata


def inspect_archive(archive_path: Path) -> list[str]:
    """Stable archive findings; no extraction, and member names are escaped in diagnostics."""
    path = Path(archive_path)
    low = path.name.lower()
    is_zip = low.endswith((".whl", ".zip"))
    is_tar = low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))
    if not (is_zip or is_tar):
        return ["unsupported_archive_extension"]
    issues: list[str] = []
    try:
        if path.stat().st_size > _MAX_ARCHIVE_BYTES:
            return ["archive_size_limit"]
        listed = _list_zip(path, issues) if is_zip else _list_tar(path, issues)
    except (OSError, EOFError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError, NotImplementedError):
        return ["unreadable_archive"]
    if isinstance(listed, list):
        return listed
    raw, metadata = listed
    normalized = []
    for name in raw:
        candidate, error = _normalize(name)
        if error:
            issues.append(f"{error}:{safe_text(name)}")
        elif candidate is not None:
            normalized.append(candidate)
    if not normalized:
        issues.append("no_valid_archive_members")
    for name in normalized:
        if _private_path(name):
            issues.append(f"private_archive_path:{safe_text(name)}")
    for name, body in metadata.items():
        if len(body) > _MAX_METADATA_BYTES:
            issues.append(f"metadata_size_limit:{safe_text(name)}")
        elif _metadata_mentions_private(_metadata_kind(name) or "lines", body):
            issues.append(f"private_metadata_reference:{safe_text(name)}")
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
