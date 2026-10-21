"""Isolated execution (M2). Execution is enabled only after the isolation is *measured*.

``DockerSandbox.probe()`` runs a probe inside a container started with exactly the flags
real runs use and checks, from the inside, what the container can do. Every required check
must pass or ``run`` refuses. There is no host fallback: when isolation cannot be shown, code
does not run. ``DisabledSandbox`` is what a mission gets when no backend is configured.

The command comes from ``VERIFICATION_COMMANDS`` (selected by the operator's spec); no
provider-supplied argv ever reaches ``docker``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .redact import redact_text

MAX_OUTPUT = 64 * 1024
_IMAGE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
_MEMORY = "256m"
_PIDS = "64"

# Runs inside the container. Prints one JSON object describing what the process can do.
_PROBE = r"""
import json, os, socket
r = {}
s = open('/proc/self/status').read()
f = dict(l.split(':', 1) for l in s.splitlines() if ':' in l)
r['uid'] = os.getuid(); r['gid'] = os.getgid()
r['cap_eff'] = f['CapEff'].strip(); r['cap_prm'] = f['CapPrm'].strip(); r['cap_bnd'] = f['CapBnd'].strip()
r['no_new_privs'] = f.get('NoNewPrivs', '').strip(); r['seccomp'] = f.get('Seccomp', '').strip()
r['interfaces'] = sorted(os.listdir('/sys/class/net'))
try:
    c = socket.socket(); c.settimeout(2); c.connect(('192.0.2.1', 9)); r['egress'] = 'open'
except OSError:
    r['egress'] = 'blocked'
def writable(p):
    try:
        open(p, 'w').close(); os.unlink(p); return True
    except OSError:
        return False
r['root_writable'] = writable('/petron-probe'); r['work_writable'] = writable('/work/.petron-probe')
r['docker_socket'] = any(os.path.exists(p) for p in ('/var/run/docker.sock', '/run/docker.sock'))
r['env_names'] = sorted(os.environ)
def first(*ps):
    for p in ps:
        try:
            return open(p).read().strip()
        except OSError:
            pass
    return None
r['pids_max'] = first('/sys/fs/cgroup/pids.max', '/sys/fs/cgroup/pids/pids.max')
r['memory_max'] = first('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes')
print(json.dumps(r))
"""
_PETRON_ENV = frozenset({"PYTHONDONTWRITEBYTECODE", "HOME", "HOSTNAME"})


class SandboxUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ProbeReport:
    backend: str
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"backend": self.backend, "ok": self.ok, "checks": self.checks, "info": self.info,
                "reason": self.reason}


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out,
                "stdout": self.stdout, "stderr": self.stderr}


class DisabledSandbox:
    backend = "none"

    def probe(self) -> ProbeReport:
        return ProbeReport("none", False, reason="no_isolation_backend")

    def run(self, workspace: Path, argv: tuple[str, ...], timeout_s: int) -> RunResult:
        raise SandboxUnavailable("no_isolation_backend")


def _cap(text: str) -> str:
    text = redact_text(text)
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n[truncated]"


class DockerSandbox:
    backend = "docker"

    def __init__(self, image: str, docker: str | None = None):
        if not _IMAGE.fullmatch(image):
            raise ValueError("image must be pinned by digest")
        self.image = image
        self.docker = docker or shutil.which("docker")
        self._verified: ProbeReport | None = None

    def _env(self) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        return env

    def _cli(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout,
                              env=self._env(), stdin=subprocess.DEVNULL)

    def _flags(self, workspace: Path, name: str) -> list[str]:
        source = str(Path(workspace).resolve(strict=True))
        if "," in source or "=" in source:
            raise SandboxUnavailable("unsupported_workspace_path")
        return ["run", "--rm", "--name", name, "--pull", "never", "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--user", "65534:65534", "--pids-limit", _PIDS, "--memory", _MEMORY, "--memory-swap", _MEMORY,
                "--cpus", "1", "--ulimit", "nofile=256:256", "--log-driver", "none", "--hostname", "petron",
                "--mount", f"type=bind,source={source},target=/work,readonly", "--workdir", "/work",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", self.image]

    def probe(self) -> ProbeReport:
        report = ProbeReport("docker", False)
        if not self.docker:
            report.reason = "docker_cli_missing"
            return report
        try:
            server = self._cli(["version", "--format", "{{.Server.Version}}"], 20)
            if server.returncode != 0:
                report.reason = "docker_daemon_unreachable"
                return report
            report.info["server_version"] = server.stdout.strip()
            sec = self._cli(["info", "--format", "{{json .SecurityOptions}}"], 20)
            options = json.loads(sec.stdout or "[]") if sec.returncode == 0 else []
            report.info["security_options"] = options
            report.info["rootless"] = any("rootless" in option for option in options)
            report.checks["daemon_seccomp"] = any("seccomp" in option for option in options)
            image = self._cli(["image", "inspect", "--format", "{{json .Config.Env}}", self.image], 20)
            report.checks["image_present_pinned"] = image.returncode == 0
            if image.returncode != 0:
                report.reason = "image_not_present"
                return report
            image_env = sorted(item.split("=", 1)[0] for item in (json.loads(image.stdout or "[]") or []))
            report.info["image_env_names"] = image_env
            with tempfile.TemporaryDirectory(prefix="petron-probe-") as workdir:
                os.chmod(workdir, 0o755)
                name = f"petron-probe-{secrets.token_hex(6)}"
                proc = self._cli([*self._flags(Path(workdir), name), "python", "-I", "-c", _PROBE], 60)
            if proc.returncode != 0:
                report.reason = "probe_container_failed"
                report.info["stderr"] = _cap(proc.stderr)[:2000]
                return report
            inside = json.loads(proc.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            report.reason = "probe_error"
            return report
        report.info["inside"] = inside
        report.checks.update({
            "non_root_uid": inside["uid"] != 0 and inside["gid"] != 0,
            "no_effective_capabilities": int(inside["cap_eff"], 16) == 0,
            "no_permitted_capabilities": int(inside["cap_prm"], 16) == 0,
            "empty_bounding_set": int(inside["cap_bnd"], 16) == 0,
            "no_new_privileges": inside["no_new_privs"] == "1",
            "seccomp_filter_active": inside["seccomp"] == "2",
            "loopback_only": inside["interfaces"] == ["lo"],
            "egress_blocked": inside["egress"] == "blocked",
            "root_fs_read_only": inside["root_writable"] is False,
            "workspace_read_only": inside["work_writable"] is False,
            "no_docker_socket": inside["docker_socket"] is False,
            # Exactly the pinned image's own ENV plus what PETRON sets: nothing from the host leaks in.
            "only_expected_env": set(inside["env_names"]) <= set(image_env) | _PETRON_ENV,
            "pids_limited": inside["pids_max"] not in (None, "max"),
            "memory_limited": inside["memory_max"] not in (None, "max")
                              and int(inside["memory_max"]) <= 256 * 1024 * 1024,
        })
        report.ok = all(report.checks.values())
        if not report.ok:
            report.reason = "isolation_check_failed:" + ",".join(sorted(k for k, v in report.checks.items() if not v))
        self._verified = report if report.ok else None
        return report

    def run(self, workspace: Path, argv: tuple[str, ...], timeout_s: int) -> RunResult:
        if self._verified is None:
            raise SandboxUnavailable("isolation_not_verified")
        name = f"petron-run-{secrets.token_hex(6)}"
        command = [self.docker, *self._flags(workspace, name), *argv]
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=self._env(), stdin=subprocess.DEVNULL)
        except OSError:
            raise SandboxUnavailable("docker_exec_failed") from None
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return RunResult(proc.returncode, _cap(out), _cap(err), False)
        except subprocess.TimeoutExpired:
            self._cli(["kill", name], 20)
            out, err = proc.communicate(timeout=30)
            return RunResult(-1, _cap(out), _cap(err), True)
