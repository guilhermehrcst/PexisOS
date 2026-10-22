"""V0.2 agent loop: contract, adaptation, limits, authorization and recovery."""

import json
import unittest

from petron.agent import DecisionInvalid, DecisionProposal, classify, validate_decision
from petron.engine import EngineError
from petron.mission import MissionState, scope_document, sha256_json
from petron.providers.agent_scripted import POLICIES, ScriptedAgentProvider
from petron.report import build_report
from petron.tools import summarize_unittest
from petron.workspace import snapshot

from support import AgentCase, e002_spec

S = MissionState


class DecisionContractTests(unittest.TestCase):
    def test_valid_decisions(self):
        validate_decision({"kind": "tool_call", "tool": "read_file", "args": {"path": "a.py"}})
        validate_decision({"kind": "request_verification"})
        validate_decision({"kind": "conclude", "summary": "done"})
        validate_decision({"kind": "stop", "reason": "x", "category": "impossible"})
        validate_decision({"kind": "request_approval", "reason": "x", "requested_paths": ["tests/a.py"]})

    def test_fabricated_results_and_permissions_are_rejected(self):
        for decision in (
            {"kind": "tool_call", "tool": "read_file", "args": {}, "observation": {"ok": True}},
            {"kind": "tool_call", "tool": "read_file", "args": {}, "permissions": ["shell"]},
            {"kind": "conclude", "summary": "ok", "tests_passed": True},
            {"kind": "grant", "tool": "shell"},
            {"kind": "stop", "reason": "x", "category": "bored"},
            {"kind": "conclude"},
            "run tests",
        ):
            with self.subTest(decision=decision), self.assertRaises(DecisionInvalid):
                validate_decision(decision)

    def test_error_taxonomy(self):
        cases = {
            "test_failure": {"ok": True, "result": {"exit_code": 1, "timed_out": False}},
            "execution_error": {"ok": True, "result": {"exit_code": -1, "timed_out": True}},
            "permission_denied": {"ok": False, "error": "path_not_writable"},
            "invalid_request": {"ok": False, "error": "unknown_tool"},
            "missing_context": {"ok": False, "error": "not_a_file"},
            "execution_unavailable": {"ok": False, "error": "isolation_unavailable:no_isolation_backend"},
            "limit_reached": {"ok": False, "error": "tool_budget_exhausted"},
            "tool_error": {"ok": False, "error": "stale_file_version"},
        }
        for expected, outcome in cases.items():
            self.assertEqual(classify(outcome), expected)
        self.assertIsNone(classify({"ok": True, "result": {"exit_code": 0, "timed_out": False}}))

    def test_unittest_summary_parser(self):
        output = ("FAIL: test_x (tests.test_m.T.test_x)\nERROR: test_y (tests.test_m.T.test_y)\n"
                  "Ran 6 tests in 0.01s\n\nFAILED (failures=1, errors=1)\n")
        summary = summarize_unittest(output)
        self.assertEqual((summary["tests_run"], summary["failures"], summary["errors"]), (6, 1, 1))
        self.assertEqual([f["id"] for f in summary["failed_tests"]], ["tests.test_m.T.test_x", "tests.test_m.T.test_y"])


class AdaptiveLoopTests(AgentCase):
    def test_e002_adaptive_observes_adapts_and_completes(self):
        source_before = snapshot(self.source)
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.COMPLETED)
        turns = self.turns(mission_id)
        kinds = [t["decision"]["kind"] for t in turns]
        self.assertEqual(kinds[-2:], ["request_verification", "conclude"])
        failures = [t["observation"]["result"]["summary"]["failures"] for t in turns
                    if t["decision"].get("tool") == "run_tests"]
        self.assertEqual(failures[:2], [2, 3])        # first observation, then the decoy made it worse
        self.assertEqual(failures[-1], 0)
        rationales = " ".join(t["decision"].get("rationale", "") for t in turns)
        self.assertIn("revert", rationales)          # adaptation after a contradicting observation
        report = build_report(self.engine.store, mission_id)
        self.assertEqual(report["verification"]["changed_paths"], ["intervals/merge.py"])
        self.assertTrue(all(report["verification"]["checks"].values()))
        self.assertEqual(snapshot(self.source), source_before)
        self.assertTrue(report["event_chain"]["ok"])
        self.assertEqual(report["mode"], "agent")
        self.assertEqual(len(report["agent_turns"]), len(turns))

    def test_every_turn_is_persisted_with_a_checkpoint(self):
        mission_id = self.start()
        self.engine.run(mission_id)
        kinds = [e["kind"] for e in self.engine.store.events(mission_id)]
        self.assertEqual(kinds.count("agent.turn"), kinds.count("agent.checkpoint"))
        self.assertIn("run.lease", kinds)


class ScopeAuthorizationTests(AgentCase):
    def test_scope_is_derived_from_the_spec_and_bound_to_approval(self):
        mission_id = self.start(approve=False)
        row = self.engine.store.mission(mission_id)
        scope = json.loads(row["plan_json"])
        self.assertEqual(scope, scope_document(self.engine.store.spec(mission_id)))
        self.assertEqual(row["plan_sha256"], sha256_json(scope))
        with self.assertRaises(EngineError) as caught:
            self.engine.run(mission_id)
        self.assertEqual(caught.exception.code, "approval_missing")
        with self.assertRaises(EngineError) as caught:
            self.engine.approve(mission_id, "0" * 64, "tester", "unit-test")
        self.assertEqual(caught.exception.code, "plan_hash_mismatch")
        self.assertEqual(self.sandbox.runs, [])

    def test_any_change_of_authority_changes_the_scope_hash(self):
        base = scope_document_for(self.source)
        for change in ({"permissions": {"tools": ["list_files"]}}, {"budget_iter": 5}):
            spec = e002_spec(self.source)
            if "permissions" in change:
                spec["permissions"] = change["permissions"]
            else:
                spec["budget"]["max_iterations"] = 5
            from petron.mission import MissionSpec
            self.assertNotEqual(sha256_json(scope_document(MissionSpec.parse(spec))), base)

    def test_tampered_scope_fails_closed(self):
        mission_id = self.start()
        raw = self.engine.store.raw()
        raw.execute("DROP TRIGGER missions_plan_guard")
        scope = json.loads(self.engine.store.mission(mission_id)["plan_json"])
        scope["tools"].append("pexisos.list_entities")
        original_sha = self.engine.store.mission(mission_id)["plan_sha256"]
        # (a) widened scope with a recomputed hash: the recorded approval no longer matches
        raw.execute("UPDATE missions SET plan_json = ?, plan_sha256 = ? WHERE mission_id = ?",
                    (json.dumps(scope), sha256_json(scope), mission_id))
        with self.assertRaises(EngineError) as caught:
            self.engine.run(mission_id)
        self.assertEqual(caught.exception.code, "approval_missing")
        # (b) widened scope under the approved hash: integrity check fails the mission closed
        raw.execute("UPDATE missions SET plan_sha256 = ? WHERE mission_id = ?", (original_sha, mission_id))
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "plan_integrity")
        self.assertEqual(self.sandbox.runs, [])

    def test_agent_mode_requires_an_iteration_limit(self):
        from petron.mission import MissionSpec
        from petron.schema import SchemaError
        spec = e002_spec(self.source)
        del spec["budget"]["max_iterations"]
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)


def scope_document_for(source):
    from petron.mission import MissionSpec
    return sha256_json(scope_document(MissionSpec.parse(e002_spec(source))))


class AdversarialPolicyTests(AgentCase):
    def test_protected_tests_cannot_be_edited(self):
        mission_id = self.start("e002_cheater")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "agent_stopped:permission")
        first = self.turns(mission_id)[0]["observation"]
        self.assertEqual((first["error_class"], first["error"]), ("permission_denied", "path_not_writable"))
        work = self.engine.home / "workspaces" / mission_id / "work" / "tests" / "test_merge.py"
        self.assertEqual(work.read_text(), (self.source / "tests" / "test_merge.py").read_text())

    def test_unverified_conclusion_is_refused(self):
        mission_id = self.start("e002_premature", max_iterations=3)
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "limit:max_iterations")
        self.assertTrue(all(t["observation"]["error_class"] == "verification_required"
                            for t in self.turns(mission_id)))

    def test_repeated_identical_actions_stop_for_lack_of_progress(self):
        mission_id = self.start("e002_stubborn")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "limit:no_progress")
        classes = [t["observation"]["error_class"] for t in self.turns(mission_id)]
        self.assertEqual(classes, ["test_failure", "test_failure", "no_progress", "no_progress"])
        self.assertEqual(len(self.sandbox.runs), 3)   # baseline + 2 executed; refusals never ran

    def test_self_granted_permissions_and_fabricated_results_are_rejected(self):
        mission_id = self.start("e002_escalate")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "limit:invalid_decisions")
        decisions = [e for e in self.engine.store.events(mission_id) if e["kind"] == "tool.authorization"]
        self.assertEqual(decisions, [])               # nothing reached the gateway

    def test_out_of_scope_request_is_a_handoff(self):
        mission_id = self.start("e002_handoff")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "handoff:approval_requested")

    def test_unpermitted_registered_tool_is_denied_by_the_gateway(self):
        mission_id = self.start("e002_unpermitted")
        self.engine.run(mission_id)
        first = self.turns(mission_id)[0]["observation"]
        self.assertEqual((first["error_class"], first["error"]), ("permission_denied", "tool_not_permitted"))


class IsolationUnavailableAgentTests(AgentCase):
    sandbox_ok = False

    def test_no_sandbox_means_no_execution_and_no_success(self):
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "agent_stopped:impossible")
        observation = [t for t in self.turns(mission_id) if t["decision"].get("tool") == "run_tests"][0]["observation"]
        self.assertEqual(observation["error_class"], "execution_unavailable")
        self.assertEqual(self.sandbox.runs, [])


class Crash(BaseException):
    """Simulates the process dying mid-mission (not caught by the engine)."""


class WrappedProvider:
    def __init__(self, policy, on_turn):
        self.inner, self.on_turn = ScriptedAgentProvider(policy), on_turn
        self.kind, self.simulated = "deterministic", True

    def next_decision(self, ctx):
        self.on_turn(ctx)
        return self.inner.next_decision(ctx)


class BudgetedFakeModel:
    """A non-simulated provider double: counts against model-call and token budgets."""
    kind, simulated = "fake-model", False

    def next_decision(self, ctx):
        return DecisionProposal({"kind": "tool_call", "tool": "list_files", "args": {}, "rationale": "look"},
                                {"provider": "fake-model", "simulated": False,
                                 "usage": {"input_tokens": 600, "output_tokens": 100}})


class ControlAndRecoveryTests(AgentCase):
    def use_provider(self, provider):
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: provider

    def test_operator_cancel_interrupts_the_loop(self):
        def cancel_at_three(ctx):
            if ctx.iteration == 3:
                self.engine.cancel(ctx.mission_id)
        self.use_provider(WrappedProvider("e002_adaptive", cancel_at_three))
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.CANCELLED)
        self.assertLessEqual(len(self.turns(mission_id)), 3)

    def test_provider_failure_fails_closed(self):
        def explode(ctx):
            raise RuntimeError("network down")
        self.use_provider(WrappedProvider("e002_adaptive", explode))
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "provider_error:RuntimeError")

    def test_model_call_and_token_budgets(self):
        self.use_provider(BudgetedFakeModel())
        spec = e002_spec(self.source, max_model_calls=2)
        spec["mission_id"] = "calls"
        mission_id = self.engine.create(spec)
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "limit:max_model_calls")
        spec = e002_spec(self.source, max_model_calls=8, max_total_tokens=1000)
        spec["mission_id"] = "tokens"
        mission_id = self.engine.create(spec)
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "limit:max_total_tokens")

    def test_interrupted_mission_resumes_from_its_checkpoint(self):
        def crash_at_seven(ctx):
            if ctx.iteration == 7 and not getattr(self, "crashed", False):
                self.crashed = True
                raise Crash()
        self.use_provider(WrappedProvider("e002_adaptive", crash_at_seven))
        mission_id = self.start()
        with self.assertRaises(Crash):
            self.engine.run(mission_id)
        self.assertEqual(self.engine.store.state(mission_id), S.RUNNING)
        turns_before = len(self.turns(mission_id))
        self.assertEqual(self.engine.resume(mission_id), S.COMPLETED)
        iterations = [t["iteration"] for t in self.turns(mission_id)]
        self.assertEqual(iterations, list(range(1, len(iterations) + 1)))   # numbering continues, no gaps
        self.assertGreater(len(iterations), turns_before)
        self.assertIn("agent.resumed", [e["kind"] for e in self.engine.store.events(mission_id)])

    def test_resume_refuses_when_the_workspace_diverged(self):
        def crash_at_four(ctx):
            if ctx.iteration == 4:
                raise Crash()
        self.use_provider(WrappedProvider("e002_adaptive", crash_at_four))
        mission_id = self.start()
        with self.assertRaises(Crash):
            self.engine.run(mission_id)
        target = self.engine.home / "workspaces" / mission_id / "work" / "intervals" / "merge.py"
        target.write_text(target.read_text() + "\n# changed outside the loop\n")
        self.assertEqual(self.engine.resume(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "resume_refused:workspace_diverged")

    def test_live_lease_blocks_a_second_runner(self):
        mission_id = self.start()
        self.engine.store.append_event(mission_id, "run.lease", {"pid": 1, "host": __import__("socket").gethostname(),
                                                                 "token": "other"})
        self.assertTrue(self.engine.store.lease_alive(mission_id))   # pid 1 is alive and not us
        from petron.store import StoreError
        with self.assertRaises(StoreError):
            self.engine.store.acquire_lease(mission_id)

    def test_resume_is_refused_for_real_providers(self):
        self.use_provider(BudgetedFakeModel())
        mission_id = self.start(max_model_calls=4)
        self.engine.store.transition(mission_id, S.AWAITING_APPROVAL, S.RUNNING)
        with self.assertRaises(EngineError) as caught:
            self.engine.resume(mission_id)
        self.assertEqual(caught.exception.code, "resume_unsupported_for_real_provider")


class PolicyRegistryTests(unittest.TestCase):
    def test_policies_are_listed(self):
        self.assertIn("e002_adaptive", POLICIES)


if __name__ == "__main__":
    unittest.main()
