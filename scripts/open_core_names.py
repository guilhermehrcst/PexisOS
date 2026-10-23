"""One definition of the private PexisOS names, shared by the D1 guards.

Repository tooling only (never imported by the runtime); standard library only.

Python packaging treats distribution names that differ only in case and in runs of
``-``, ``_`` and ``.`` as the same project (PEP 503 / "name normalization"), and an
import name, a module file or a ``*.dist-info`` directory can spell the same root.
Every comparison therefore goes through :func:`canonical_name`; no guard keeps its
own spelling list or regular expression.
"""

from __future__ import annotations

import re

PRIVATE_DISTRIBUTIONS = frozenset({"pexisos-enterprise", "pexisos-cloud"})

_SEPARATORS = re.compile(r"[-_.]+")
# A name token: what PEP 508 allows in a project name plus the dots of a module path.
_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# "<name>-<version>" as used by sdist roots, *.dist-info, *.data and *.egg-info.
_VERSIONED = re.compile(r"^(?P<name>.+?)-\d[^/]*$")
_METADATA_DIR_SUFFIXES = (".dist-info", ".data", ".egg-info")


def canonical_name(name: str) -> str:
    """PEP 503 normalization: lowercase, every run of ``-_.`` becomes one ``-``."""
    return _SEPARATORS.sub("-", name).lower()


def is_private_distribution(name: str) -> bool:
    return canonical_name(name.strip()) in PRIVATE_DISTRIBUTIONS


def mentions_private_name(text: str) -> bool:
    """True if any name-like token in ``text`` is, or starts with, a private root.

    Covers requirement strings (``PexisOS.Cloud[x]>=1; marker``), entry-point targets
    (``pexisos_cloud.app:main``) and module paths. A token is checked as a whole and at
    each of its dotted prefixes, so ``pexisos_cloud.client`` and ``pexisos.enterprise``
    both match while ``pexisos-cloudy`` or ``my-pexisos-cloud-adapter`` do not.
    """
    for token in _TOKEN.findall(text):
        parts = token.split(".")
        if any(is_private_distribution(".".join(parts[:end])) for end in range(1, len(parts) + 1)):
            return True
    return False


def is_private_path_segment(segment: str) -> bool:
    """True if one path segment names a private module, package or distribution.

    Recognised forms: a package directory or gitlink (``pexisos_cloud``), a module file
    of any kind (``pexisos_cloud.py``, ``.pth``, ``.pyd``, ``.cpython-312-…so``,
    ``.abi3.so``) and versioned metadata or source roots (``pexisos_cloud-1.0.dist-info``,
    ``pexisos.cloud-1.0.data``, ``pexisos_cloud-1.0`` inside an sdist).
    """
    lowered = segment.lower()
    stem = lowered.split(".", 1)[0]
    if is_private_distribution(stem):
        return True
    for suffix in _METADATA_DIR_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    versioned = _VERSIONED.match(lowered)
    return is_private_distribution(versioned.group("name") if versioned else lowered)


def safe_text(value: str) -> str:
    """Render an untrusted name on one line: control characters and non-ASCII escaped.

    Diagnostics carry archive member names and Git paths; this keeps a crafted name
    from forging extra log lines, CI workflow commands or terminal escapes.
    """
    return value.encode("unicode_escape", "backslashreplace").decode("ascii")
