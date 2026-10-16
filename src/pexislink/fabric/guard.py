"""Runtime barrier: request handling cannot create effects outside the process.

A second, independent layer behind the static architecture tests. While
``no_effects()`` is active on the current thread, an audit hook refuses
sockets, process creation, native-library loading, filesystem mutation, file
writes and opening device or kernel-interface paths. The hook is process-wide
and permanent once installed (CPython cannot remove audit hooks), but it is
inert unless the current thread is inside ``no_effects()``.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class EffectBlocked(RuntimeError):
    """Raised when guarded code attempts an external effect."""


_state = threading.local()
_install_lock = threading.Lock()
_installed = False

_BLOCKED_PREFIXES = ("socket.", "subprocess.", "ctypes.", "os.exec", "os.spawn", "os.posix_spawn", "winreg.")
_BLOCKED_EVENTS = frozenset({
    "os.system", "os.fork", "os.forkpty", "os.startfile", "os.kill", "os.killpg", "os.remove", "os.unlink",
    "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.symlink", "os.link", "os.truncate",
    "os.utime", "shutil.rmtree", "shutil.copyfile", "shutil.move", "urllib.Request", "webbrowser.open",
    "pty.spawn", "sys.addaudithook",
})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_DEVICE_PREFIXES = ("/dev/", "/sys/", "/proc/")


def _hook(event: str, args: tuple) -> None:
    if not getattr(_state, "depth", 0):
        return
    if event in _BLOCKED_EVENTS or event.startswith(_BLOCKED_PREFIXES):
        raise EffectBlocked("effect_blocked")
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            raise EffectBlocked("effect_blocked")
        if isinstance(flags, int) and flags & _WRITE_FLAGS:
            raise EffectBlocked("effect_blocked")
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if isinstance(path, str) and os.path.abspath(path).startswith(_DEVICE_PREFIXES):
            raise EffectBlocked("effect_blocked")


def _install() -> None:
    global _installed
    with _install_lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True


@contextmanager
def no_effects() -> Iterator[None]:
    _install()
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1
