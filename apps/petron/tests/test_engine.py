import unittest

from petron.engine import EngineError
from petron.mission import MissionState
from petron.report import SIMULATED_NOTICE, build_report, render_markdown
from petron.workspace import snapshot

from support import EngineCase

S = MissionState


class HappyPathTests(EngineCase):
    def test_e001_flow_completes_with_evidence(self):
        source_before = snapshot(self.source)
        mission_id, plan_sha = self.planned()
        self.assertEqual(self.engine.store.state(mission_id), S.AWAITING_APPROVAL)
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.COMPLETED)
        self.assertEqual(snapshot(self.source), source_before)
        report = build_report(self.engine.store, mission_id)
        self.assertTrue(report["simulated_provider"])
        self.assertEqual(report["notice"], SIMULATED_NOTICE)
        self.assertEqual(report["baseline_tests"]["exit_code"], 1)  # defect reproduced before the change
        self.assertTrue(all(report["verification"]["checks"].values()))
        self.assertEqual(report["verification"]["changed_paths"], ["calc/stats.py"])
        self.assertIn("+    return (ordered[middle - 1] + ordered[middle]) / 2", report["diff"])
        self.assertTrue(report["event_chain"]["ok"])
        self.assertIn("SIMULATED PROVIDER", render_markdown(report))
        states = [(a, b) for a, b, _ in report["states"]]
        self.assertEqual(states, [("created", "planning"), ("planning", "awaiting_approval"),
                                  ("awaiting_approval", "running"), ("running", "verifying"),
                                  ("verifying", "completed")])


class ApprovalGateTests(EngineCase):
    def test_run_without_approval_is_refused_and_state_unchanged(self):
        mission_id, _ = self.planned()
        with self.assertRaises(EngineError) as caught:
            self.engine.run(mission_id)
        self.assertEqual(caught.exception.code, "approval_missing")
        self.assertEqual(self.engine.store.state(mission_id), S.AWAITING_APPROVAL)
        self.assertEqual(self.sandbox.runs, [])

    def test_approval_for_a_different_plan_hash_is_refused(self):
        mission_id, plan_sha = self.planned()
        with self.assertRaises(EngineError) as caught:
            self.engine.approve(mission_id, "0" * 64, "tester", "unit-test")
        self.assertEqual(caught.exception.code, "plan_hash_mismatch")

    def test_second_run_loses_the_race(self):
        mission_id, plan_sha = self.planned()
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        self.engine.run(mission_id)
        with self.assertRaises(EngineError):
            self.engine.run(mission_id)

    def test_cancel_from_awaiting_approval_and_terminal_is_final(self):
        mission_id, _ = self.planned()
        self.engine.cancel(mission_id)
        self.assertEqual(self.engine.store.state(mission_id), S.CANCELLED)
        with self.assertRaises(EngineError):
            self.engine.cancel(mission_id)


class AdversarialProviderTests(EngineCase):
    def failed(self, script, approve=True):
        mission_id = self.engine.create(self.spec(script))
        try:
            plan_sha = self.engine.plan(mission_id)
        except EngineError as error:
            return mission_id, error.code
        if approve:
            self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        return mission_id, self.engine.store.mission(mission_id)["failure_reason"]

    def test_permission_escalation_through_arguments_rejected_at_plan_time(self):
        mission_id, reason = self.failed("escalate_via_args")
        self.assertTrue(reason.startswith("plan_rejected:invalid_arguments:0"))
        self.assertEqual(self.engine.store.state(mission_id), S.FAILED)

    def test_unknown_tool_rejected_at_plan_time(self):
        self.assertEqual(self.failed("unknown_tool")[1], "plan_rejected:unknown_tool:0")

    def test_unpermitted_tool_rejected_at_plan_time(self):
        self.assertEqual(self.failed("not_permitted")[1], "plan_rejected:tool_not_permitted:0")

    def test_editing_protected_tests_blocked_at_runtime_even_after_approval(self):
        mission_id, reason = self.failed("edit_protected_tests")
        self.assertEqual(reason, "step_failed:0:path_not_writable")
        self.assertEqual(self.sandbox.runs[-1].name, "base")  # only the baseline ran; no post-change test run

    def test_path_traversal_blocked_at_runtime(self):
        self.assertEqual(self.failed("path_traversal")[1], "step_failed:0:invalid_path")

    def test_wrong_fix_fails_runtime_verification(self):
        mission_id, reason = self.failed("e001_wrong_fix")
        self.assertEqual(reason, "verification_failed:tests_pass_in_sandbox")
        verdict = build_report(self.engine.store, mission_id)["verification"]
        self.assertEqual(verdict["changed_paths"], ["calc/stats.py"])
        self.assertTrue(verdict["checks"]["changes_within_writable_paths"])

    def test_noop_plan_cannot_complete(self):
        mission_id, reason = self.failed("e001_noop")
        self.assertEqual(reason, "verification_failed:tests_pass_in_sandbox")


class IsolationUnavailableTests(EngineCase):
    sandbox_ok = False

    def test_execution_disabled_mission_fails_closed(self):
        mission_id, plan_sha = self.planned()
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        reason = self.engine.store.mission(mission_id)["failure_reason"]
        self.assertEqual(reason, "step_failed:2:isolation_unavailable:fake_unavailable")
        self.assertIn("execution.disabled", self.kinds(mission_id))
        self.assertEqual(self.sandbox.runs, [])


    def test_plan_without_test_step_cannot_complete_when_isolation_is_unavailable(self):
        mission_id = self.engine.create(self.spec("e001_fix_without_tests"))
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.engine.store.mission(mission_id)["failure_reason"],
                         "verification_failed:tests_pass_in_sandbox")
        verdict = build_report(self.engine.store, mission_id)["verification"]
        self.assertEqual(verdict["tests"], {"ran": False, "reason": "isolation_unavailable"})
        self.assertEqual(self.sandbox.runs, [])


class RuntimeVerificationTests(EngineCase):
    def test_runtime_runs_tests_even_if_the_plan_does_not(self):
        mission_id = self.engine.create(self.spec("e001_fix_without_tests"))
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.COMPLETED)
        self.assertEqual([p.name for p in self.sandbox.runs], ["base", "work"])


class IndependentVerificationTests(EngineCase):
    """Verification must catch a bad workspace on its own, as if the gateway had been bypassed."""

    def verify_after_tampering(self, rel, old, new):
        from petron.mission import VERIFICATION_COMMANDS
        from petron.workspace import Workspace
        mission_id, plan_sha = self.planned()
        self.engine.approve(mission_id, plan_sha, "tester", "unit-test")
        store = self.engine.store
        store.transition(mission_id, S.AWAITING_APPROVAL, S.RUNNING)
        store.transition(mission_id, S.RUNNING, S.VERIFYING)
        base = Workspace.create(self.source, self.tmp / "area", "base")
        work = Workspace.create(self.source, self.tmp / "area", "work")
        work.replace_once("calc/stats.py", "    return ordered[middle]\n",
                          "    if len(ordered) % 2:\n        return ordered[middle]\n"
                          "    return (ordered[middle - 1] + ordered[middle]) / 2\n")
        path = work.root / rel
        path.write_text(path.read_text().replace(old, new, 1))
        state = self.engine._verify(mission_id, store.spec(mission_id), base, work, self.sandbox, True,
                                    VERIFICATION_COMMANDS["python-unittest"])
        return state, store.mission(mission_id)["failure_reason"]

    def test_modified_protected_test_file_is_caught(self):
        state, reason = self.verify_after_tampering("tests/test_stats.py", "def test_single", "def test_single_x")
        self.assertEqual(state, S.FAILED)
        self.assertEqual(reason, "verification_failed:changes_within_writable_paths,protected_paths_unchanged")

    def test_change_outside_writable_paths_is_caught(self):
        state, reason = self.verify_after_tampering("calc/__init__.py", "Synthetic", "Tampered")
        self.assertEqual(state, S.FAILED)
        self.assertEqual(reason, "verification_failed:changes_within_writable_paths")


class RealProviderGateTests(EngineCase):
    def test_real_provider_needs_explicit_authorization(self):
        spec = self.spec()
        spec["provider"] = {"kind": "anthropic", "model": "claude-opus-5-5"}
        spec["budget"]["max_model_calls"] = 1
        mission_id = self.engine.create(spec)
        with self.assertRaises(EngineError) as caught:
            self.engine.plan(mission_id)
        self.assertEqual(caught.exception.code, "provider_error:real_model_not_authorized")
        self.assertEqual(self.engine.store.state(mission_id), S.FAILED)


if __name__ == "__main__":
    unittest.main()
