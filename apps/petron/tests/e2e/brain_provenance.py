"""Shared provenance for PETRON Brain browser evidence.

Git failure is unknown provenance, never proof of a clean repository.
Executed from the e2e scripts directory without importing Playwright or Docker.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


def provenance(source: Path) -> dict:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=source, capture_output=True, text=True,
                timeout=30, check=True
            )
            return result.stdout.strip()
        head = git("rev-parse", "HEAD")
        status = git("status", "--porcelain", "--", ".")
        if not head or len(head) != 40 or any(c not in "0123456789abcdefABCDEF" for c in head):
            raise ValueError("Git did not return a valid SHA")
        return {"head": head, "dirty": bool(status), "generated_at": timestamp}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"head": None, "dirty": None, "generated_at": timestamp}
