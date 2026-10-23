"""Sandbox hardening: output is bounded while it is captured, termination is guaranteed or
reported, cancellation reaches the running container, and resource ceilings are verified exactly.

The first group drives a fake ``docker`` executable so it runs everywhere; the last group runs a
real container when a daemon and the pinned image are available (otherwise it is skipped, and a
skip is reported as not run, never as passed)."""

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
import tracemalloc
import unittest
from pathlib import Path

from petron.sandbox import MAX_OUTPUT, DockerSandbox, ProbeReport, SandboxUnavailable, isolation_checks

from test_sandbox_docker import IMAGE, SANDBOX

_FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, pathlib, signal, sys, time
    here = pathlib.Path(__file__).resolve().parent
    mode = (here / "mode.txt").read_text().strip()
    args = sys.argv[1:]
    with open(here / "calls.log", "a") as log:
        log.write(" ".join(args[:2]) + "\\n")
    def stop_run():
        try:
            os.kill(int((here / "run.pid").read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    if args[0] == "run":
        (here / "run.pid").write_text(str(os.getpid()))
        (here / "container").write_text(args[args.index("--name") + 1])
        out, err = sys.stdout.buffer, sys.stderr.buffer
        if mode.startswith("flood"):
            chunk = b"x" * 65536
            for _ in range(3072):                      # ~192 MiB per stream, as fast as the pipe allows
                out.write(chunk); err.write(chunk)
            out.flush(); err.flush()
            while True:
                time.sleep(0.2)
        elif mode.startswith("sleep"):
            while True:
                time.sleep(0.2)
        elif mode == "tail":
            out.write(b"HEAD-MARKER\\n" + b"noise\\n" * 100000 + b"Ran 7 tests in 1.2s\\nFAILED (failures=2)\\n")
            out.flush()
            os._exit(1)
        elif mode == "ok":
            out.write(b"fine\\n"); out.flush(); os._exit(0)
    elif args[0] == "kill":
        if "kill_fails" in mode:
            sys.exit(1)
        stop_run(); (here / "container").unlink(missing_ok=True)
    elif args[0] == "rm":
        if "kill_fails" in mode and "rm_fails" in mode:
            sys.exit(1)
        stop_run(); (here / "container").unlink(missing_ok=True)
    elif args[0] == "ps":
        name = (here / "container")
        if name.exists():
            print(name.read_text())
''')


class FakeDockerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-fakedocker-"))
        self.fake = self.tmp / "docker"
        self.fake.write_text(_FAKE_DOCKER)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.sandbox = DockerSandbox(IMAGE, docker=str(self.fake))
        self.sandbox._verified = ProbeReport("docker", True)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def mode(self, name):
        (self.tmp / "mode.txt").write_text(name)

    def calls(self):
        path = self.tmp / "calls.log"
        return path.read_text().splitlines() if path.exists() else []


class BoundedCaptureTests(FakeDockerCase):
    def test_sustained_output_is_bounded_while_it_is_captured(self):
        self.mode("flood")
        tracemalloc.start()
        try:
            result = self.sandbox.run(self.work, ("python", "-c", "pass"), 3)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(result.timed_out)
        self.assertLess(peak, 12 * 1024 * 1024, f"host-side peak {peak} bytes: output was buffered, not bounded")
        self.assertLessEqual(len(result.stdout), MAX_OUTPUT + 200)
        self.assertLessEqual(len(result.stderr), MAX_OUTPUT + 200)
        self.assertTrue(result.stdout_truncated and result.stderr_truncated)
        self.assertGreater(result.stdout_bytes, 20 * 1024 * 1024, "the flood did not run: test proves nothing")

    def test_the_end_of_a_long_output_is_preserved_for_test_summaries(self):
        self.mode("tail")
        result = self.sandbox.run(self.work, ("python", "-m", "unittest"), 20)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.stdout_truncated)
        self.assertIn("HEAD-MARKER", result.stdout)
        self.assertIn("Ran 7 tests in 1.2s", result.stdout)
        self.assertIn("FAILED (failures=2)", result.stdout)
        self.assertIn("omitted", result.stdout)
        self.assertLessEqual(len(result.stdout), MAX_OUTPUT + 200)
        self.assertEqual(result.to_dict()["stdout_truncated"], True)

    def test_small_output_is_untouched_and_not_marked_truncated(self):
        self.mode("ok")
        result = self.sandbox.run(self.work, ("python", "-c", "pass"), 20)
        self.assertEqual((result.exit_code, result.stdout, result.stdout_truncated), (0, "fine\n", False))
        self.assertEqual(result.stdout_bytes, 5)

    def test_secrets_in_captured_output_are_redacted(self):
        self.mode("ok")
        (self.tmp / "docker").write_text(_FAKE_DOCKER.replace(
            'out.write(b"fine\\\\n")', 'out.write(b"token=hunter2hunter2\\\\n")'))
        result = self.sandbox.run(self.work, ("python", "-c", "pass"), 20)
        self.assertNotIn("hunter2hunter2", result.stdout)


class TerminationTests(FakeDockerCase):
    def test_timeout_kills_the_container_and_confirms_it_is_gone(self):
        self.mode("sleep")
        started = time.monotonic()
        result = self.sandbox.run(self.work, ("python", "-c", "pass"), 1)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("kill", [c.split()[0] for c in self.calls()])
        self.assertFalse((self.tmp / "container").exists())

    def test_failed_kill_falls_back_to_force_removal(self):
        self.mode("sleep_kill_fails")
        result = self.sandbox.run(self.work, ("python", "-c", "pass"), 1)
        self.assertTrue(result.timed_out)
        self.assertIn("rm", [c.split()[0] for c in self.calls()])
        self.assertFalse((self.tmp / "container").exists())

    def test_unremovable_container_is_reported_not_swallowed(self):
        self.mode("sleep_kill_fails_rm_fails")
        started = time.monotonic()
        with self.assertRaises(SandboxUnavailable) as caught:
            self.sandbox.run(self.work, ("python", "-c", "pass"), 1)
        self.assertEqual(caught.exception.reason, "container_cleanup_failed")
        self.assertLess(time.monotonic() - started, 60, "cleanup must be bounded")
        # the local client process is reaped even though the daemon could not remove the container
        pid = int((self.tmp / "run.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.1)

    def test_container_gets_an_intrinsic_deadline(self):
        """Even if the host loses the daemon, the container must end by itself."""
        self.mode("ok")
        self.sandbox.run(self.work, ("python", "-c", "pass"), 30)
        self.assertIn("run --rm", (self.tmp / "calls.log").read_text())
        command = self.sandbox._deadline_wrapped(("python", "-c", "pass"), 30)
        self.assertEqual(command[:3], ("timeout", "-s", "KILL"))
        self.assertGreater(int(command[3]), 30)
        self.assertEqual(command[4:], ("python", "-c", "pass"))


class CancellationTests(FakeDockerCase):
    def test_cancel_stops_the_running_container_promptly(self):
        self.mode("sleep")
        flag = threading.Event()
        threading.Timer(0.7, flag.set).start()
        started = time.monotonic()
        result = self.sandbox.run(self.work, ("python", "-c", "pass"), 120, cancelled=flag.is_set)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse((self.tmp / "container").exists())

    def test_a_failing_cancel_probe_does_not_cancel(self):
        self.mode("ok")

        def broken():
            raise RuntimeError("store busy")
        self.assertEqual(self.sandbox.run(self.work, ("python", "-c", "pass"), 20, cancelled=broken).exit_code, 0)


class EngineCancellationTests(unittest.TestCase):
    """`mission cancel` (another process, another connection) must reach a running execution."""

    def test_cancel_during_the_baseline_run_stops_the_sandbox_and_ends_the_mission(self):
        from petron.engine import Engine
        from petron.mission import MissionState
        from petron.sandbox import RunResult
        from support import E001, FakeSandbox, e001_spec

        class Blocking(FakeSandbox):
            started = threading.Event()
            saw_cancel = threading.Event()

            def run(self, workspace, argv, timeout_s, cancelled=None):
                Blocking.started.set()
                for _ in range(200):
                    if cancelled and cancelled():
                        Blocking.saw_cancel.set()
                        return RunResult(-1, "", "", False, cancelled=True)
                    time.sleep(0.05)
                return RunResult(0, "", "", False)

        tmp = Path(tempfile.mkdtemp(prefix="petron-cancel-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        source = tmp / "source"
        shutil.copytree(E001 / "project", source)
        engine = Engine(tmp / "home", sandbox_factory=lambda spec: Blocking(True))
        self.addCleanup(engine.close)
        mission_id = engine.create(e001_spec(source))
        engine.approve(mission_id, engine.plan(mission_id), "tester", "unit-test")
        outcome = {}
        runner = threading.Thread(target=lambda: outcome.update(state=Engine(tmp / "home", sandbox_factory=lambda s: Blocking(True)).run(mission_id)))
        runner.start()
        self.assertTrue(Blocking.started.wait(10))
        Engine(tmp / "home").cancel(mission_id)
        runner.join(20)
        self.assertFalse(runner.is_alive(), "cancel did not stop the in-flight execution")
        self.assertTrue(Blocking.saw_cancel.is_set())
        self.assertEqual(engine.store.state(mission_id), MissionState.CANCELLED)
        kinds = [e["kind"] for e in engine.store.events(mission_id)]
        self.assertNotIn("verification.result", kinds, "a cancelled mission must not be verified")


class ResourceCeilingTests(unittest.TestCase):
    BASE = {"uid": 65534, "gid": 65534, "cap_eff": "0", "cap_prm": "0", "cap_bnd": "0", "no_new_privs": "1",
            "seccomp": "2", "interfaces": ["lo"], "egress": "blocked", "root_writable": False,
            "work_writable": False, "docker_socket": False, "env_names": ["PATH"], "pids_max": "64",
            "memory_max": str(256 * 1024 * 1024), "cpu_max": "100000 100000", "cpu_cfs": None,
            "has_timeout": True}

    def checks(self, **changes):
        return isolation_checks({**self.BASE, **changes}, ["PATH"])

    def test_exact_ceilings_pass(self):
        self.assertTrue(all(self.checks().values()), self.checks())

    def test_pid_ceiling_must_not_exceed_the_declared_limit(self):
        for value in ("max", "9999", "65", None, "garbage", "0"):
            self.assertFalse(self.checks(pids_max=value)["pids_limited"], value)
        self.assertTrue(self.checks(pids_max="64")["pids_limited"])
        self.assertTrue(self.checks(pids_max="32")["pids_limited"])

    def test_cpu_ceiling_is_measured_and_fails_closed(self):
        for value in ("max 100000", "200000 100000", "garbage", None, "100000 0"):
            self.assertFalse(self.checks(cpu_max=value)["cpu_limited"], value)
        self.assertTrue(self.checks(cpu_max="100000 100000")["cpu_limited"])
        self.assertTrue(self.checks(cpu_max="50000 100000")["cpu_limited"])
        legacy = {"cpu_max": None}
        self.assertTrue(self.checks(**legacy, cpu_cfs=["100000", "100000"])["cpu_limited"])
        self.assertFalse(self.checks(**legacy, cpu_cfs=["-1", "100000"])["cpu_limited"])
        self.assertFalse(self.checks(**legacy, cpu_cfs=["200000", "100000"])["cpu_limited"])
        self.assertFalse(self.checks(**legacy, cpu_cfs=None)["cpu_limited"])

    def test_memory_ceiling_garbage_fails_instead_of_raising(self):
        self.assertFalse(self.checks(memory_max="garbage")["memory_limited"])
        self.assertFalse(self.checks(memory_max=str(512 * 1024 * 1024))["memory_limited"])

    def test_missing_deadline_wrapper_disables_execution(self):
        self.assertFalse(self.checks(has_timeout=False)["intrinsic_deadline_available"])


@unittest.skipUnless(SANDBOX, "docker isolation not available or probe failed")
class RealContainerBoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-realflood-"))
        os.chmod(self.tmp, 0o755)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_sustained_container_output_keeps_host_memory_bounded(self):
        code = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stderr.write('y' * 65536)\n"
        tracemalloc.start()
        try:
            result = SANDBOX.run(self.tmp, ("python", "-I", "-c", code), 5)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(result.timed_out)
        self.assertLess(peak, 12 * 1024 * 1024)
        self.assertTrue(result.stdout_truncated)
        self.assertGreater(result.stdout_bytes, 2 * MAX_OUTPUT)
        listed = subprocess.run(["docker", "ps", "-a", "--filter", "name=petron-run-", "--format", "{{.Names}}"],
                                capture_output=True, text=True)
        self.assertEqual(listed.stdout.strip(), "", "container left behind")

    def test_a_real_probe_measures_cpu_and_pid_ceilings(self):
        report = DockerSandbox(IMAGE).probe()
        self.assertTrue(report.ok, report.reason)
        for name in ("pids_limited", "cpu_limited", "intrinsic_deadline_available"):
            self.assertTrue(report.checks[name], name)


if __name__ == "__main__":
    unittest.main()
