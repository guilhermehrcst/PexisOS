"""Test-only driver for the Codex CLI ``app-server`` JSON-RPC interface. NOT part of PexisLink.

Codex is a client here: it launches the configured PexisLink stdio server and talks MCP to it. This
driver speaks Codex's documented (experimental) app-server protocol over stdio and uses only
model-free requests: ``initialize``, ``mcpServerStatus/list``, ``thread/start`` (an ephemeral thread,
no turn is ever started) and ``mcpServer/tool/call``. No model is called and no credential exists.

Containment, two independent barriers plus detection:

1. the model provider is an unroutable local URL and every HTTP(S) proxy variable points at an
   unroutable local port, so any outbound request fails closed;
2. Codex features that sync or fetch remotely (plugins, remote plugins, apps, dependency
   installs, the daemon, updates) are disabled in the isolated ``CODEX_HOME``;
3. detection: Codex runs with ``RUST_LOG=info``, where a failed outbound request is logged; tests
   assert no such line appears (``egress_lines``).

Codex receives an explicit environment (no host variables beyond ``PATH``) and a fresh, empty
``HOME``/``CODEX_HOME`` under a temporary directory, so no user configuration or login is read.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

UNROUTABLE = "http://127.0.0.1:9"
DISABLED_FEATURES = ("plugins", "remote_plugin", "apps", "skill_mcp_dependency_install", "daemon_auto_start",
                     "in_app_updates")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_EGRESS = re.compile(r"error sending request|failed to connect|unable to access|failed to lookup|"
                     r"connection refused|dns error|tcp connect", re.IGNORECASE)


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string of printable text is a valid TOML basic string


def write_config(codex_home: Path, servers: dict[str, dict]) -> None:
    """``servers`` maps a name to {"command": str, "args": [str], "env": {str: str}}."""
    lines = ['model = "pexislink-offline-model"', 'model_provider = "pexislink-offline"', "",
             "[model_providers.pexislink-offline]", 'name = "offline (unroutable)"',
             f"base_url = {_toml_str(UNROUTABLE + '/v1')}", 'wire_api = "responses"', "", "[features]"]
    lines += [f"{name} = false" for name in DISABLED_FEATURES]
    lines += ["", "[analytics]", "enabled = false"]
    for name, spec in servers.items():
        lines += ["", f"[mcp_servers.{name}]", f"command = {_toml_str(spec['command'])}",
                  "args = [" + ", ".join(_toml_str(a) for a in spec["args"]) + "]",
                  f"startup_timeout_sec = 60", "", f"[mcp_servers.{name}.env]"]
        lines += [f"{key} = {_toml_str(value)}" for key, value in spec.get("env", {}).items()]
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def codex_env(root: Path, extra: dict | None = None) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
           "RUST_LOG": "info", "NO_PROXY": "", "no_proxy": ""}
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        env[name] = UNROUTABLE
    env.update(extra or {})
    return env


def run_cli(codex: str, root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    for name in ("home", "codex"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return subprocess.run([codex, *args], capture_output=True, text=True, timeout=timeout, env=codex_env(root),
                          cwd=str(root / "home"))


class RpcError(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("message"))
        self.error = error


class CodexAppServer:
    """One ``codex app-server`` process over stdio, with an isolated home."""

    def __init__(self, codex: str, root: Path, servers: dict[str, dict], *, env: dict | None = None,
                 timeout: float = 90):
        self.root, self.timeout = root, timeout
        (root / "home").mkdir(parents=True, exist_ok=True)
        write_config(root / "codex", servers)
        self.stderr_path = root / "app-server.log"
        self._stderr = open(self.stderr_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=codex_env(root, env), cwd=str(root / "home"),
                                     text=True, bufsize=1)
        self._messages: queue.Queue = queue.Queue()
        self.notifications: list[dict] = []
        self._next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.initialize_result = self.request("initialize", {"clientInfo": {"name": "pexislink-compat",
                                                                             "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._messages.put(json.loads(line))
        self._messages.put(None)

    def _send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=1)
            except queue.Empty:
                continue
            if message is None:
                raise RuntimeError(f"codex app-server exited during {method}")
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RpcError(message["error"])
                return message["result"]
            if "method" in message and "id" in message:  # a server-to-client request: never approve
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not supported by the compatibility driver"}})
            self.notifications.append(message)
        raise TimeoutError(method)

    # model-free requests ----------------------------------------------------------------------
    def status(self, server: str | None = None) -> list[dict]:
        params = {"serverName": server} if server else {}
        return self.request("mcpServerStatus/list", params)["data"]

    def start_thread(self) -> str:
        return self.request("thread/start", {"ephemeral": True})["thread"]["id"]

    def call(self, thread: str, server: str, tool: str, arguments: dict) -> dict:
        return self.request("mcpServer/tool/call", {"threadId": thread, "server": server, "tool": tool,
                                                    "arguments": arguments})

    # lifecycle and evidence -------------------------------------------------------------------
    def close(self, timeout: float = 30) -> tuple[int | None, float]:
        started = time.monotonic()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            code = None
        self.proc.stdout.close()
        self._stderr.close()
        return code, time.monotonic() - started

    def log(self) -> str:
        return _ANSI.sub("", self.stderr_path.read_text(encoding="utf-8", errors="replace"))

    def egress_lines(self) -> list[str]:
        """Log lines that report an outbound request (each would have failed on the barriers)."""
        return [line for line in self.log().splitlines() if _EGRESS.search(line)]

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *exc) -> None:
        if self.proc.poll() is None:
            self.close()
