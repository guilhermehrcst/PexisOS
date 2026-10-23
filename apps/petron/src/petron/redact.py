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
_NAME = r"[A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd)[A-Za-z0-9_]*"
# A quoted value may hold spaces, escaped quotes and newlines (bounded, so a missing closing quote
# cannot make the match run away); an unquoted value stops at whitespace.
_DQ = r'"(?:[^"\\]|\\.){4,4096}"'
_SQ = r"'(?:[^'\\]|\\.){4,4096}'"
_ESCAPED_DQ = r'\\"(?:[^"\\]|\\(?!"))+\\"'       # password=\"two words\" inside a JSON string
_UNTERMINATED = r"[\"'][^\"'\r\n]{4,}(?=\r?\n|$)"    # quote opened, never closed on this line
_VALUE = rf"(?:{_ESCAPED_DQ}|{_DQ}|{_SQ}|[^\s'\"]{{4,}}|{_UNTERMINATED})"
_ASSIGNMENT = re.compile(rf"(?i)\b({_NAME})(\s*[=:]\s*){_VALUE}")
_JSON_STRING = re.compile(rf'(?i)("{_NAME}"\s*:\s*){_DQ}')


def _mask_assignment(match: re.Match) -> str:
    name, separator = match.group(1), match.group(2)
    value = match.group(0)[len(name) + len(separator):]
    if name.lower().endswith("tokens") and value.isdigit():
        return match.group(0)   # a token *count* (max_tokens, input_tokens...), not a credential
    return f"{name}{separator}{REDACTED}"


def _env_secret_values() -> list[str]:
    values = [value for name, value in os.environ.items()
              if _SECRET_ENV_NAME.search(name) and value and len(value) >= 8]
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str) -> str:
    for value in _env_secret_values():
        text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _JSON_STRING.sub(lambda m: f"{m.group(1)}\"{REDACTED}\"", text)
    return _ASSIGNMENT.sub(_mask_assignment, text)


def contains_secret(value: Any) -> bool:
    """True when redaction would change anything in ``value`` (strings inside JSON-like data)."""
    if isinstance(value, str):
        return redact_text(value) != value
    if isinstance(value, dict):
        return any(contains_secret(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item) for item in value)
    return False


def redact(value: Any) -> Any:
    """Recursively redact strings inside JSON-like data (keys are kept)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
