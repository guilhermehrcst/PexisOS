"""V0.2 hardening: what the operator approved is what runs, spends and verifies.

Covers (review findings, Codex P1/P2 on PR #24): provider settings bound into the approved scope,
an aggregate token cap that is enforced *before* each external call, no hidden retries, cancellation
that beats a slow model response, unique runner leases, source integrity before the loop, and
recovery that trusts only what was fully committed."""

import json
import os
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from petron.agent import AgentLoop, DecisionProposal
from petron.engine import Engine, EngineError
from petron.mission import MissionSpec, MissionState, scope_document
from petron.providers import build_agent_provider, build_provider
from petron.providers.anthropic_provider import AnthropicAgentProvider, request_input_upper_bound
from petron.providers.base import ProviderError
from petron.report import build_report, render_markdown
from petron.schema import SchemaError
from petron.store import StoreError
from petron.tools import summarize_unittest

from support import AgentCase, e002_spec
from test_agent import Crash, WrappedProvider
from test_providers import ScriptedClient, use

S = MissionState


def reply(content, input_tokens=100, output_tokens=50, stop_reason="tool_use"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
                           _request_id="req_test")


def real_spec(source, **budget):
    spec = e002_spec(source)
    spec["provider"] = {"kind": "anthropic"}
    spec["budget"].update(max_model_calls=8, max_iterations=8, max_total_tokens=200000)
    spec["budget"].update(budget)
    return spec


class RealAgentCase(AgentCase):
    """E002 with a 'real' provider whose HTTP client is a recording fake. No network, ever."""

    def use_client(self, client, **provider_args):
        def factory(config, allow_real_model=False):
            self.factory_config = dict(config)
            return AnthropicAgentProvider(config["model"], config["effort"], config["max_tokens"], client=client)
        self.engine.agent_provider_factory = factory

    def approved(self, spec):
        mission_id = self.engine.create(spec)
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        return mission_id


# --------------------------------------------------------------------------------------------
class ProviderSettingsBindingTests(RealAgentCase):
    def test_defaults_are_resolved_and_persisted_before_approval(self):
        mission_id = self.engine.create(real_spec(self.source))
        stored = json.loads(self.engine.store.mission(mission_id)["spec_json"])["provider"]
        self.assertEqual(set(stored), {"kind", "model", "effort", "max_tokens"})
        scope = scope_document(self.engine.store.spec(mission_id))
        self.assertEqual(scope["provider"], stored)

    def test_the_approved_scope_hash_covers_every_effective_setting(self):
        hashes = set()
        for index, change in enumerate(({}, {"effort": "low"}, {"model": "claude-sonnet-5-5"}, {"max_tokens": 8000})):
            spec = real_spec(self.source)
            spec["mission_id"] = f"bind-{index}"
            spec["provider"].update(change)
            mission_id = self.engine.create(spec)
            hashes.add(self.engine.plan(mission_id))
        self.assertEqual(len(hashes), 4)

    def test_changing_defaults_after_approval_cannot_change_the_approved_terms(self):
        client = ScriptedClient([reply([use("petron_stop", {"reason": "x", "category": "impossible"}, "t1")])])
        self.use_client(client)
        mission_id = self.approved(real_spec(self.source))
        approved = dict(json.loads(self.engine.store.mission(mission_id)["spec_json"])["provider"])
        with mock.patch.dict("petron.mission.PROVIDER_DEFAULTS",
                             {"model": "claude-haiku-5-5", "effort": "low", "max_tokens": 1000}):
            self.engine.run(mission_id, allow_real_model=True)
        self.assertEqual(self.factory_config, approved, "execution used settings other than the approved ones")
        self.assertEqual(client.requests[0]["model"], approved["model"])
        self.assertEqual(client.requests[0]["output_config"], {"effort": approved["effort"]})

    def test_a_row_stored_without_resolved_settings_is_refused_when_defaults_change(self):
        """A mission approved before this hardening holds an unresolved provider block."""
        legacy = real_spec(self.source)
        legacy["mission_id"] = "legacy-claude"
        canonical = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        import hashlib
        raw = self.engine.store.raw()
        raw.execute("INSERT INTO missions (mission_id, spec_json, spec_sha256, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'awaiting_approval', 't', 't')",
                    ("legacy-claude", canonical, hashlib.sha256(canonical.encode()).hexdigest()))
        with self.assertRaises(StoreError) as caught:
            self.engine.store.spec("legacy-claude")
        self.assertEqual(caught.exception.code, "spec_integrity")

    def test_factories_refuse_an_unbound_provider_block(self):
        for factory in (build_provider, build_agent_provider):
            for config in ({"kind": "anthropic"}, {"kind": "anthropic", "model": "claude-opus-5-5"},
                           {"kind": "anthropic", "model": "claude-opus-5-5", "effort": "high"}):
                with self.assertRaises(ProviderError) as caught:
                    factory(config, allow_real_model=True)
                self.assertEqual(caught.exception.code, "provider_settings_unbound")

    def test_a_provider_that_does_not_match_the_approved_scope_never_starts_the_mission(self):
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: AnthropicAgentProvider(
            "claude-haiku-5-5", config["effort"], config["max_tokens"], client=ScriptedClient([]))
        mission_id = self.approved(real_spec(self.source))
        with self.assertRaises(EngineError) as caught:
            self.engine.run(mission_id, allow_real_model=True)
        self.assertEqual(caught.exception.code, "provider_settings_changed")
        self.assertEqual(self.engine.store.state(mission_id), S.AWAITING_APPROVAL)


class AggregateTokenBudgetTests(RealAgentCase):
    def test_a_real_agent_mission_without_a_total_token_cap_cannot_be_created(self):
        spec = real_spec(self.source)
        del spec["budget"]["max_total_tokens"]
        with self.assertRaises(SchemaError) as caught:
            self.engine.create(spec)
        self.assertIn("max_total_tokens", str(caught.exception))
        self.assertIn("aggregate", str(caught.exception))

    def test_per_request_tokens_and_iterations_are_not_a_substitute(self):
        spec = real_spec(self.source)
        del spec["budget"]["max_total_tokens"]
        spec["provider"]["max_tokens"] = 256
        spec["budget"]["max_iterations"] = 1
        with self.assertRaises(SchemaError):
            MissionSpec.parse(spec)

    def test_the_deterministic_provider_is_unaffected(self):
        MissionSpec.parse(e002_spec(self.source))


class TokenCapBeforeCallTests(RealAgentCase):
    def run_with(self, responses, **budget):
        client = ScriptedClient(responses)
        self.use_client(client)
        mission_id = self.approved(real_spec(self.source, **budget))
        state = self.engine.run(mission_id, allow_real_model=True)
        return mission_id, state, client

    def test_every_request_fits_the_remaining_budget_by_construction(self):
        budget, used = 60000, 0
        responses = [reply([use("list_files", {}, f"t{i}")], input_tokens=9000, output_tokens=9000)
                     for i in range(6)]
        client = ScriptedClient(responses)
        self.use_client(client)
        mission_id = self.approved(real_spec(self.source, max_total_tokens=budget))
        self.engine.run(mission_id, allow_real_model=True)
        self.assertTrue(client.requests)
        for request in client.requests:
            bound = request_input_upper_bound(request["system"], request["tools"], request["messages"])
            self.assertLessEqual(bound + request["max_tokens"], budget - used,
                                 f"a request could have overrun the aggregate cap (used so far {used})")
            used += 18000
        self.assertLessEqual(used, budget + 18000)
        self.assertEqual(self.engine.store.mission(mission_id)["state"], "failed")

    def test_a_large_configured_output_is_capped_to_what_remains(self):
        client = ScriptedClient([reply([use("list_files", {}, "t1")], 100, 100)])
        self.use_client(client)
        spec = real_spec(self.source, max_total_tokens=30000)
        spec["provider"]["max_tokens"] = 32000
        mission_id = self.approved(spec)
        self.engine.run(mission_id, allow_real_model=True)
        first = client.requests[0]
        self.assertLess(first["max_tokens"], 32000)
        self.assertLessEqual(first["max_tokens"] + request_input_upper_bound(
            first["system"], first["tools"], first["messages"]), 30000)

    def test_a_request_that_cannot_fit_is_refused_without_calling_the_provider(self):
        mission_id, state, client = self.run_with([], max_total_tokens=1000)
        self.assertEqual(client.requests, [], "an HTTP request was made although the cap could not hold it")
        self.assertEqual(state, S.FAILED)
        self.assertEqual(self.reason(mission_id), "provider_error:token_budget_insufficient")

    def test_an_oversized_context_is_refused_before_the_call(self):
        client = ScriptedClient([reply([use("read_file", {"path": "intervals/merge.py"}, "t1")], 100, 100),
                                 reply([use("list_files", {}, "t2")], 100, 100)])
        self.use_client(client)
        mission_id = self.approved(real_spec(self.source, max_total_tokens=12000))
        with mock.patch("petron.agent.OBSERVATION_TEXT_LIMIT", 10 ** 6), \
                mock.patch.object(AgentLoop, "_observe_tool",
                                  lambda self_, d, t: {"ok": True, "error_class": None, "error": None,
                                                       "result": "x" * 40000}):
            self.engine.run(mission_id, allow_real_model=True)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.reason(mission_id), "provider_error:token_budget_insufficient")

    def test_the_iteration_that_would_exceed_the_cap_does_not_start(self):
        _, state, client = self.run_with([reply([use("list_files", {}, "t1")], 400, 400)] * 4,
                                         max_total_tokens=20000)
        for request in client.requests:
            self.assertLessEqual(request["max_tokens"], 20000)
        self.assertEqual(state, S.FAILED)


class NoHiddenRetryTests(RealAgentCase):
    def test_the_agent_client_is_built_without_sdk_retries(self):
        seen = {}

        class Anthropic:
            def __init__(self, **kwargs):
                seen.update(kwargs)
        fake = __import__("types").ModuleType("anthropic")
        fake.Anthropic = Anthropic
        os.environ["ANTHROPIC_API_KEY"] = "fake-key-for-test-only"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        with mock.patch.dict("sys.modules", {"anthropic": fake}):
            AnthropicAgentProvider("claude-opus-5-5", "high", 1000)._build_client()
        self.assertEqual(seen["max_retries"], 0)

    def test_a_transient_failure_is_one_request_and_is_accounted(self):
        client = ScriptedClient([TimeoutError("server accepted it, then went quiet")])
        self.use_client(client)
        mission_id = self.approved(real_spec(self.source))
        state = self.engine.run(mission_id, allow_real_model=True)
        self.assertEqual(len(client.requests), 1, "one decision must be one HTTP attempt")
        self.assertEqual(state, S.FAILED)
        failure = [e["payload"] for e in self.engine.store.events(mission_id)
                   if e["kind"] == "agent.provider_error"][-1]
        self.assertEqual(failure["model_calls"], 1, "the attempt was not counted against the model-call budget")
        self.assertEqual(failure["code"], "api_error:TimeoutError")

    def test_usage_of_a_refused_or_truncated_response_is_not_lost(self):
        for stop_reason, code in (("refusal", "model_refusal"), ("max_tokens", "model_truncated")):
            with self.subTest(stop_reason):
                client = ScriptedClient([reply([], 700, 300, stop_reason)])
                self.use_client(client)
                spec = real_spec(self.source)
                spec["mission_id"] = f"usage-{stop_reason}"
                mission_id = self.approved(spec)
                self.engine.run(mission_id, allow_real_model=True)
                failure = [e["payload"] for e in self.engine.store.events(mission_id)
                           if e["kind"] == "agent.provider_error"][-1]
                self.assertEqual(failure["code"], code)
                self.assertEqual((failure["model_calls"], failure["tokens"]), (1, 1000))
                report = build_report(self.engine.store, mission_id)
                self.assertEqual(report["usage"]["model_calls"], 1)
                self.assertEqual(report["usage"]["input_tokens"] + report["usage"]["output_tokens"], 1000)


# --------------------------------------------------------------------------------------------
class CancellationTests(AgentCase):
    def use_provider(self, provider):
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: provider

    def after_baseline(self):
        """Sandbox runs in the working copy: the baseline (before the loop) ran in `base`, legitimately."""
        return [path for path in self.sandbox.runs if path.name == "work"]

    class SlowThenVerify:
        """A model whose slow answer arrives after the operator cancelled."""
        kind, simulated = "deterministic", True

        def __init__(self, home, decision):
            self.home, self.decision = home, decision

        def next_decision(self, ctx):
            Engine(self.home).cancel(ctx.mission_id)          # another process, another connection
            return DecisionProposal(self.decision, {"provider": "deterministic", "simulated": True})

    def test_request_verification_after_a_cancel_runs_nothing(self):
        self.use_provider(self.SlowThenVerify(self.engine.home, {"kind": "request_verification"}))
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.CANCELLED)
        self.assertEqual(self.after_baseline(), [], "a sandbox command started after the operator cancelled")
        kinds = [e["kind"] for e in self.engine.store.events(mission_id)]
        self.assertNotIn("verification.result", kinds)
        self.assertNotIn("agent.turn", kinds)

    def test_conclude_after_a_cancel_never_enters_verification(self):
        self.use_provider(self.SlowThenVerify(self.engine.home, {"kind": "conclude", "summary": "done"}))
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.CANCELLED)
        states = [e["payload"]["to"] for e in self.engine.store.events(mission_id) if e["kind"] == "mission.state"]
        self.assertNotIn("verifying", states)

    def test_a_tool_call_after_a_cancel_is_denied_and_nothing_runs(self):
        self.use_provider(self.SlowThenVerify(self.engine.home, {"kind": "tool_call", "tool": "run_tests",
                                                                  "args": {}}))
        mission_id = self.start()
        self.assertEqual(self.engine.run(mission_id), S.CANCELLED)
        self.assertEqual(self.after_baseline(), [])

    def test_the_final_verification_is_not_started_if_cancel_arrives_before_it(self):
        engine = self.engine
        mission_id = self.start()
        original = Engine._verify

        def cancel_then_verify(self_, *args, **kwargs):
            Engine(engine.home).cancel(mission_id)
            return original(self_, *args, **kwargs)
        with mock.patch.object(Engine, "_verify", cancel_then_verify):
            state = engine.run(mission_id)
        self.assertEqual(state, S.CANCELLED)


# --------------------------------------------------------------------------------------------
class LeaseTests(AgentCase):
    def make_mission(self, name):
        spec = e002_spec(self.source)
        spec["mission_id"] = name
        mission_id = self.engine.create(spec)
        self.engine.approve(mission_id, self.engine.plan(mission_id), "tester", "unit-test")
        return mission_id

    def test_two_engines_in_one_process_cannot_both_own_a_mission(self):
        for round_ in range(30):
            mission_id = self.make_mission(f"lease-race-{round_}")
            barrier, results = threading.Barrier(2), []

            def contend():
                engine = Engine(self.engine.home)   # one connection per thread, same process, same pid
                try:
                    barrier.wait()
                    results.append(("won", engine.store.acquire_lease(mission_id)["token"]))
                except StoreError as error:
                    results.append(("lost", error.code))
                finally:
                    engine.close()
            threads = [threading.Thread(target=contend) for _ in range(2)]
            [t.start() for t in threads]
            [t.join(10) for t in threads]
            self.assertEqual(sorted(kind for kind, _ in results), ["lost", "won"], (round_, results))
            self.assertIn(next(v for k, v in results if k == "lost"), {"lease_held"})

    def test_same_pid_does_not_mean_same_lease(self):
        mission_id = self.make_mission("lease-pid")
        first = self.engine.store.acquire_lease(mission_id)
        other = Engine(self.engine.home)
        self.addCleanup(other.close)
        with self.assertRaises(StoreError) as caught:
            other.store.acquire_lease(mission_id)
        self.assertEqual(caught.exception.code, "lease_held")
        self.assertTrue(self.engine.store.lease_owned(mission_id, first["token"]))

    def test_a_released_lease_can_be_taken_again(self):
        mission_id = self.make_mission("lease-release")
        first = self.engine.store.acquire_lease(mission_id)
        self.engine.store.release_lease(mission_id, first["token"])
        second = self.engine.store.acquire_lease(mission_id)
        self.assertNotEqual(first["token"], second["token"])
        self.assertFalse(self.engine.store.lease_owned(mission_id, first["token"]))

    def test_a_crashed_runner_in_this_process_does_not_block_forever(self):
        """A run that unwound without releasing (its token is no longer registered) is stale."""
        mission_id = self.make_mission("lease-crash")
        self.engine.store.append_event(mission_id, "run.lease", {"pid": os.getpid(), "host": socket.gethostname(),
                                                                 "token": "never-registered-here"})
        self.assertFalse(self.engine.store.lease_alive(mission_id))
        self.engine.store.acquire_lease(mission_id)

    def test_a_dead_process_lease_is_stale_and_a_live_one_or_another_host_blocks(self):
        mission_id = self.make_mission("lease-pids")
        store = self.engine.store
        child = __import__("subprocess").Popen(["true"])
        child.wait()
        store.append_event(mission_id, "run.lease", {"pid": child.pid, "host": socket.gethostname(), "token": "dead"})
        store.acquire_lease(mission_id)                                   # stale: taken over
        store.append_event(mission_id, "run.lease", {"pid": os.getppid(), "host": socket.gethostname(),
                                                     "token": "alive"})
        with self.assertRaises(StoreError):
            store.acquire_lease(mission_id)
        store.append_event(mission_id, "run.lease", {"pid": 1, "host": "another-host", "token": "far"})
        with self.assertRaises(StoreError) as caught:
            store.acquire_lease(mission_id)
        self.assertEqual(caught.exception.code, "lease_held_elsewhere")

    def test_a_runner_that_lost_its_lease_stops_before_spending_anything_more(self):
        mission_id = self.start()
        engine, calls = self.engine, []

        def steal_after_first(ctx):
            calls.append(ctx.iteration)
            if ctx.iteration == 2:
                engine.store.append_event(mission_id, "run.lease", {"pid": os.getppid(), "host": socket.gethostname(),
                                                                     "token": "taken-over"})
        engine.agent_provider_factory = lambda config, allow_real_model=False: WrappedProvider(
            "e002_adaptive", steal_after_first)
        state = engine.run(mission_id)
        # The mission now belongs to the runner that took the lease; this one must neither spend nor
        # fail it (failing it would kill the legitimate run).
        self.assertEqual(state, S.RUNNING)
        self.assertEqual(calls, [1, 2], "the superseded runner kept calling the provider")
        outcome = [e["payload"] for e in engine.store.events(mission_id) if e["kind"] == "agent.outcome"][-1]
        self.assertEqual(outcome["reason"], "lease_lost")

    def test_concurrent_resumes_run_the_mission_once(self):
        def crash(ctx):
            if ctx.iteration == 4 and not getattr(self, "crashed", False):
                self.crashed = True
                raise Crash()
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: WrappedProvider("e002_adaptive", crash)
        mission_id = self.start()
        with self.assertRaises(Crash):
            self.engine.run(mission_id)
        calls, gate = [], threading.Barrier(2)

        class Counting:
            kind, simulated = "deterministic", True

            def __init__(self):
                from petron.providers.agent_scripted import ScriptedAgentProvider
                self.inner = ScriptedAgentProvider("e002_adaptive")

            def next_decision(self, ctx):
                calls.append(ctx.iteration)
                return self.inner.next_decision(ctx)
        outcomes = []

        def resume():
            engine = Engine(self.engine.home, sandbox_factory=lambda spec: self.sandbox,
                            agent_provider_factory=lambda c, allow_real_model=False: Counting())
            try:
                gate.wait()
                outcomes.append(engine.resume(mission_id))
            except EngineError as error:
                outcomes.append(error.code)
            finally:
                engine.close()
        threads = [threading.Thread(target=resume) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        self.assertEqual(calls, sorted(set(calls)), f"two runners drove the same iterations: {calls}")
        self.assertEqual(sum(1 for o in outcomes if o == S.COMPLETED), 1, outcomes)
        self.assertEqual(sum(1 for o in outcomes if o == "lease_held"), 1, outcomes)


# --------------------------------------------------------------------------------------------
class SourceIntegrityBeforeTheLoopTests(AgentCase):
    class Counting:
        kind, simulated = "deterministic", True

        def __init__(self):
            self.calls = 0

        def next_decision(self, ctx):
            self.calls += 1
            return DecisionProposal({"kind": "stop", "reason": "x", "category": "impossible"}, {"simulated": True})

    def test_a_source_changed_after_approval_never_reaches_the_provider(self):
        provider = self.Counting()
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: provider
        mission_id = self.start()
        (self.source / "intervals" / "merge.py").write_text("# altered after the scope was approved\n")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(provider.calls, 0, "altered files reached the model loop")
        self.assertEqual(self.sandbox.runs, [])
        self.assertIn("source_changed", self.reason(mission_id))

    def test_a_symlink_added_after_approval_never_reaches_the_provider(self):
        provider = self.Counting()
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: provider
        mission_id = self.start()
        os.symlink("/etc/passwd", self.source / "intervals" / "leak.py")
        self.assertEqual(self.engine.run(mission_id), S.FAILED)
        self.assertEqual(provider.calls, 0)


# --------------------------------------------------------------------------------------------
class RecoveryIntegrityTests(AgentCase):
    def interrupted(self, at=5):
        def crash(ctx):
            if ctx.iteration == at and not getattr(self, "crashed", False):
                self.crashed = True
                raise Crash()
        self.engine.agent_provider_factory = lambda config, allow_real_model=False: WrappedProvider("e002_adaptive", crash)
        mission_id = self.start()
        with self.assertRaises(Crash):
            self.engine.run(mission_id)
        return mission_id

    def test_a_turn_after_the_last_checkpoint_is_not_restored(self):
        mission_id = self.interrupted()
        last = [e["payload"] for e in self.engine.store.events(mission_id) if e["kind"] == "agent.checkpoint"][-1]
        # the process died between the committed turn and its checkpoint
        self.engine.store.append_event(mission_id, "agent.turn", {
            "iteration": last["iteration"] + 1, "provider": {},
            "decision": {"kind": "tool_call", "tool": "run_tests", "args": {}},
            "observation": {"ok": False, "error_class": "test_failure", "workspace_changed": False}})
        self.assertEqual(self.engine.resume(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "resume_refused:uncheckpointed_turn")

    def test_a_corrupted_event_chain_is_refused_before_any_state_is_restored(self):
        mission_id = self.interrupted()
        raw = self.engine.store.raw()
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json = REPLACE(payload_json, '\"tool_calls\":1', '\"tool_calls\":0') "
                    "WHERE kind = 'agent.checkpoint' AND mission_id = ?", (mission_id,))
        self.assertEqual(self.engine.resume(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "resume_refused:event_chain_broken")

    def test_a_modified_baseline_is_refused_on_resume(self):
        mission_id = self.interrupted()
        base = self.engine.home / "workspaces" / mission_id / "base" / "intervals" / "merge.py"
        base.write_text(base.read_text() + "\n# the baseline was edited while interrupted\n")
        self.assertEqual(self.engine.resume(mission_id), S.FAILED)
        self.assertEqual(self.reason(mission_id), "resume_refused:baseline_diverged")

    def test_a_crash_during_final_verification_is_recoverable(self):
        engine = self.engine
        mission_id = self.start()

        def die_inside_verification(self_, *args, **kwargs):
            raise Crash()
        with mock.patch.object(Engine, "_verify", die_inside_verification):
            with self.assertRaises(Crash):
                engine.run(mission_id)
        self.assertEqual(engine.store.state(mission_id), S.VERIFYING)
        self.assertEqual(engine.resume(mission_id), S.COMPLETED)
        verdicts = [e for e in engine.store.events(mission_id) if e["kind"] == "verification.result"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(engine.store.artifact(mission_id, "diff.patch")["sha256"],
                         verdicts[0]["payload"]["diff_sha256"])
        self.assertIn("verification.recovered", [e["kind"] for e in engine.store.events(mission_id)])

    def test_recovery_after_a_partially_written_verification_does_not_collide(self):
        engine = self.engine
        mission_id = self.start()

        def half_then_die(self_, mission, spec, base, work, sandbox, isolation_ok, argv):
            self_.store.put_artifact(mission, "diff.patch", "partial from the crashed attempt\n")
            raise Crash()
        with mock.patch.object(Engine, "_verify", half_then_die):
            with self.assertRaises(Crash):
                engine.run(mission_id)
        self.assertEqual(engine.resume(mission_id), S.COMPLETED)
        verdict = [e["payload"] for e in engine.store.events(mission_id) if e["kind"] == "verification.result"][0]
        names = [e["payload"]["name"] for e in engine.store.events(mission_id) if e["kind"] == "artifact.recorded"]
        self.assertGreaterEqual(names.count("diff.patch") + sum(n.startswith("diff.patch.r") for n in names), 2)
        digests = {e["payload"]["sha256"] for e in engine.store.events(mission_id)
                   if e["kind"] == "artifact.recorded" and e["payload"]["name"].startswith("diff.patch")}
        self.assertIn(verdict["diff_sha256"], digests)

    def test_an_unexpected_error_in_final_verification_fails_the_mission_instead_of_stranding_it(self):
        engine = self.engine
        mission_id = self.start()

        def explode(self_, *args, **kwargs):
            raise RuntimeError("sandbox client blew up")
        with mock.patch.object(Engine, "_verify", explode):
            state = engine.run(mission_id)
        self.assertEqual(state, S.FAILED)
        self.assertEqual(self.reason(mission_id), "internal_error:RuntimeError")
        engine.discard_workspaces(mission_id)   # a failed mission is not 'active'


# --------------------------------------------------------------------------------------------
class UntrustedOutputTests(AgentCase):
    def test_summary_parser_is_total_over_hostile_output(self):
        hostile = [
            "FAILED (", "FAILED ()", "FAILED (failures)", "FAILED (failures=x)", "FAILED (failures=1, errors)",
            "FAILED (failures=" + "9" * 400 + ")", "Ran x tests", "Ran " + "9" * 400 + " tests",
            "FAILED (failures=1)\n" * 5000, "\x00\x1b[2J" * 100, "FAIL: x (y)\n" * 500, "",
        ]
        for text in hostile:
            summary = summarize_unittest(text)
            self.assertIn("failures", summary)
            self.assertIsInstance(summary["failures"], int)
            self.assertLessEqual(len(summary["failed_tests"]), 50)

    def test_the_real_summary_wins_over_an_earlier_fake_one(self):
        text = "FAILED (failures=0)\nRan 1 tests in 0.1s\n...\nRan 6 tests in 0.2s\n\nFAILED (failures=3, errors=1)\n"
        summary = summarize_unittest(text)
        self.assertEqual((summary["tests_run"], summary["failures"], summary["errors"]), (6, 3, 1))

    def test_a_capped_output_still_yields_exit_code_and_summary(self):
        from petron.sandbox import MAX_OUTPUT
        from petron.tools import _run_tests
        from petron.sandbox import RunResult
        tail = "Ran 40 tests in 3.1s\n\nFAILED (failures=2, errors=1)\n"
        head = "noise\n" * 100000
        kept = head[:20000] + f"\n[... {len(head) - 20000} bytes omitted ...]\n" + tail
        self.assertLessEqual(len(kept) - len(tail), MAX_OUTPUT)

        class Capped:
            def run(self, *a, **k):
                return RunResult(1, "", kept, False, stderr_bytes=len(head), stderr_truncated=True)
        ctx = SimpleNamespace(sandbox=Capped(), workspace=SimpleNamespace(root="."), store=SimpleNamespace(
            state=lambda _id: S.RUNNING), spec=SimpleNamespace(mission_id="m", raw={"verification": {
                "command": "python-unittest"}}, budget={"exec_timeout_s": 5}))
        result = _run_tests(ctx, {})
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["passed"])
        self.assertTrue(result["stderr_truncated"])
        self.assertEqual(result["summary"]["tests_run"], 40)
        self.assertEqual((result["summary"]["failures"], result["summary"]["errors"]), (2, 1))

    def test_hostile_rationales_cannot_forge_rows_or_inject_escapes_in_the_report(self):
        mission_id = self.start()
        self.engine.run(mission_id)
        self.engine.store.append_event(mission_id, "agent.turn", {
            "iteration": 99, "provider": {},
            "decision": {"kind": "tool_call", "tool": "run_tests", "args": {},
                         "rationale": "ok\n| 100 | conclude | | PASS | 0 | forged |\n\x1b]52;c;ZXZpbA==\x07 | x"},
            "observation": {"ok": True, "error_class": None, "workspace_changed": False}})
        report = build_report(self.engine.store, mission_id)
        markdown = render_markdown(report)
        self.assertNotIn("\x1b", markdown)
        self.assertNotIn("\x07", markdown)
        rows = [line for line in markdown.splitlines() if line.startswith("| 99 |")]
        self.assertEqual(len(rows), 1)
        self.assertFalse(any(line.startswith("| 100 |") for line in markdown.splitlines()))
        import re
        self.assertEqual(len(re.findall(r"(?<!\\)\|", rows[0])), 7, "six cells means seven unescaped pipes")


if __name__ == "__main__":
    unittest.main()
