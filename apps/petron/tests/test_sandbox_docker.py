"""Real isolation tests. They run only where a Docker daemon and the pinned image exist and the
probe passes; otherwise they are skipped (reported as NOT_RUN, never as passed)."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from petron.engine import Engine
from petron.mission import MissionState
from petron.report import build_report
from petron.sandbox import DockerSandbox, SandboxUnavailable

from support import E001, e001_spec

IMAGE = (
    "mirror.gcr.io/library/python@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8")


def _probe():
    if not shutil.which("docker"):
        return None
    sandbox = DockerSandbox(IMAGE)
    report = sandbox.probe()
    return sandbox if report.ok else None


SANDBOX = _probe()


class FailClosedProbeTests(unittest.TestCase):
    """Run everywhere: a missing daemon or image must disable execution, never fall back."""

    def test_unreachable_daemon_disables_execution(self):
        sandbox = DockerSandbox(IMAGE, docker="/bin/false")
        report = sandbox.probe()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "docker_daemon_unreachable")
        with self.assertRaises(SandboxUnavailable):
            sandbox.run(Path(tempfile.gettempdir()), ("python", "-c", "pass"), 5)

    def test_missing_cli_disables_execution(self):
        sandbox = DockerSandbox(IMAGE)
        sandbox.docker = None
        self.assertEqual(sandbox.probe().reason, "docker_cli_missing")

    @unittest.skipUnless(SANDBOX, "needs a reachable daemon")
    def test_unknown_image_disables_execution(self):
        report = DockerSandbox("mirror.gcr.io/library/python@sha256:" + "0" * 64).probe()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "image_not_present")

    def test_unpinned_image_rejected(self):
        with self.assertRaises(ValueError):
            DockerSandbox("python:3.11-slim")


@unittest.skipUnless(SANDBOX, "docker isolation not available or probe failed")
class DockerSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-docker-"))
        self.work = self.tmp / "work"
        shutil.copytree(E001 / "project", self.work)
        for path in [self.work, *self.work.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, code, timeout=30):
        return SANDBOX.run(self.work, ("python", "-I", "-c", code), timeout)

    def test_unverified_sandbox_refuses_to_run(self):
        with self.assertRaises(SandboxUnavailable):
            DockerSandbox(IMAGE).run(self.work, ("python", "-c", "pass"), 5)

    def test_baseline_reproduces_the_defect(self):
        result = SANDBOX.run(self.work, ("python", "-I", "-B", "-m", "unittest", "discover", "-s", "tests",
                                         "-t", ".", "-v"), 60)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("test_even_length", result.stderr)
        self.assertIn("FAILED (failures=1)", result.stderr)

    def test_workspace_mount_is_read_only(self):
        before = (self.work / "calc" / "stats.py").read_bytes()
        result = self.run_py("open('/work/calc/stats.py', 'a').write('x')")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual((self.work / "calc" / "stats.py").read_bytes(), before)

    def test_no_network(self):
        result = self.run_py("import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)")
        self.assertNotEqual(result.exit_code, 0)

    def test_process_limit_holds(self):
        code = ("import threading, time\n"
                "ts = [threading.Thread(target=time.sleep, args=(5,)) for _ in range(200)]\n"
                "[t.start() for t in ts]\n")
        result = self.run_py(code)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("can't start new thread", result.stderr)

    def test_timeout_kills_the_container(self):
        result = self.run_py("import time; time.sleep(60)", timeout=3)
        self.assertTrue(result.timed_out)
        listed = subprocess.run(["docker", "ps", "-a", "--filter", "name=petron-run-", "--format", "{{.Names}}"],
                                capture_output=True, text=True)
        self.assertEqual(listed.stdout.strip(), "")


@unittest.skipUnless(SANDBOX, "docker isolation not available or probe failed")
class DockerEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-docker-e2e-"))
        self.source = self.tmp / "source"
        shutil.copytree(E001 / "project", self.source)
        self.engine = Engine(self.tmp / "home")

    def tearDown(self):
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mission(self, script):
        spec = e001_spec(self.source, sandbox={"backend": "docker", "image": IMAGE})
        spec["provider"] = {"kind": "deterministic", "script": script}
        mission_id = self.engine.create(spec)
        plan_sha = self.engine.plan(mission_id)
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        return mission_id, self.engine.run(mission_id)

    def test_e001_completes_in_real_sandbox(self):
        mission_id, state = self.mission("e001_fix_median")
        report = build_report(self.engine.store, mission_id)
        self.assertEqual(state, MissionState.COMPLETED, report["failure_reason"])
        self.assertEqual(report["baseline_tests"]["exit_code"], 1)
        self.assertTrue(report["sandbox_probe"]["ok"])
        self.assertEqual(report["verification"]["tests"]["exit_code"], 0)

    def test_e002_agent_loop_completes_in_real_sandbox(self):
        from support import E002, e002_spec
        source = self.tmp / "e002"
        shutil.copytree(E002 / "project", source)
        spec = e002_spec(source)
        spec["sandbox"] = {"backend": "docker", "image": IMAGE}
        mission_id = self.engine.create(spec)
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        state = self.engine.run(mission_id)
        report = build_report(self.engine.store, mission_id)
        self.assertEqual(state, MissionState.COMPLETED, report["failure_reason"])
        failures = [t["failures"] for t in report["agent_turns"] if t["tool"] == "run_tests"]
        self.assertEqual((failures[0], failures[1], failures[-1]), (2, 3, 0))   # measured in the container
        self.assertEqual(report["verification"]["changed_paths"], ["intervals/merge.py"])

    def test_wrong_fix_fails_in_real_sandbox(self):
        mission_id, state = self.mission("e001_wrong_fix")
        self.assertEqual(state, MissionState.FAILED)
        self.assertEqual(self.engine.store.mission(mission_id)["failure_reason"],
                         "verification_failed:tests_pass_in_sandbox")


if __name__ == "__main__":
    unittest.main()
