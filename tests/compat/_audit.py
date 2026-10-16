"""Test-only audit-event classifier shared by the fixture server (child) and the tests (parent).

Records every CPython audit event in the security-relevant families, so nothing is missed by an
incomplete allowlist: all ``socket.*``, process creation and control, native loading, and file
opens whose normalized path is a device or kernel interface.
"""

from __future__ import annotations

import os

PREFIXES = ("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "ctypes.", "winreg.")
EVENTS = frozenset({"os.system", "os.fork", "os.forkpty", "os.kill", "os.killpg", "os.startfile", "pty.spawn",
                    "webbrowser.open", "urllib.Request"})
DEVICE_PREFIXES = ("/dev/", "/sys/", "/proc/")


def describe(event: str, args: tuple) -> str | None:
    """A one-line description of a watched event, or None when the event is not watched."""
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if not isinstance(path, str):
            return None
        normalized = os.path.abspath(path)
        if normalized.startswith(DEVICE_PREFIXES) or normalized in ("/dev", "/sys", "/proc"):
            return f"open {normalized} flags={flags} mode={mode}"
        return None
    if event.startswith(PREFIXES) or event in EVENTS:
        if event == "socket.__new__":
            family = args[1] if len(args) > 1 else None
            return f"socket.__new__ family={int(family) if family is not None else None}"
        if event in ("os.kill", "os.killpg"):
            return f"{event} target={args[0]} signal={args[1] if len(args) > 1 else None}"
        return event
    return None
