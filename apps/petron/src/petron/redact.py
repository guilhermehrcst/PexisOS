"""Secret redaction applied to every string before it is persisted or printed.

Defense in depth, not a guarantee: known token shapes, ``key = value`` assignments, and the
literal values of secret-looking environment variables are replaced. Callers still must not
pass secrets to tools in the first place.
"""

from __future__ import annotations

import os
import re
from typing import Any

REDACTED = "[REDACTED]"
_SECRET_ENV_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)")
_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd)[A-Za-z0-9_]*)(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3")


def _env_secret_values() -> list[str]:
    values = [value for name, value in os.environ.items()
              if _SECRET_ENV_NAME.search(name) and value and len(value) >= 8]
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str) -> str:
    for value in _env_secret_values():
        text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact(value: Any) -> Any:
    """Recursively redact strings inside JSON-like data (keys are kept)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
