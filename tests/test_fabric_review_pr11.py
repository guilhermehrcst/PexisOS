"""Regression tests for the automated review of PR #11 (one test per finding)."""

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fabric_helpers import ALICE, BOB, CAR, DAY, FRIDGE, fridge_obs, grant, world

from pexislink.fabric import ContractError, Fabric, FabricError, ManualClock
from pexislink.fabric.adapters import appliance
from pexislink.fabric.adapters._common import T0
from pexislink.fabric.assess import AssessmentConfig
from pexislink.fabric.policy import AuditLog, grants_to_document, load_policy_file
from pexislink.fabric.runtime import tool_specs, FabricLimits


class FreshnessFollowsUsableEvidenceTests(unittest.TestCase):
    """P1: a fresh 'unknown' record must not make day-old measurements look current."""

    def test_fresh_unknown_after_stale_measurements_is_still_stale(self):
        fabric = Fabric(clock=ManualClock(T0 + DAY + 60_000_000))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        for i in range(30):
            fabric.ingest(fridge_obs(i))  # all a day old
        fresh_unknown = fridge_obs(24 * 60, payload={})
        fresh_unknown["quality"]["status"] = "unknown"
        self.assertTrue(fabric.ingest(fresh_unknown).accepted)
        result = fabric.open_session(ALICE).call("explain_assessment", {"entity_id": FRIDGE})
        self.assertEqual(result["state"], "stale")
        self.assertTrue(result["freshness"]["stale"])
        self.assertEqual(result["signals"][0]["state"], "stale")

    def test_per_signal_freshness(self):
        fabric = Fabric(clock=ManualClock(T0 + 40 * 60_000_000))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        for i in range(40):  # temperature reported throughout ...
            payload = {"temperature_c": 4.0} if i >= 25 else {"temperature_c": 4.0, "cycle_count": 1}
            fabric.ingest(fridge_obs(i, payload=payload))
        result = fabric.open_session(ALICE).call("explain_assessment",
                                                 {"entity_id": FRIDGE, "signal": "temperature_c"})
        self.assertEqual(result["signals"][0]["state"], "nominal")


class FiniteParametersTests(unittest.TestCase):
    """P2: non-finite assessment parameters are rejected at construction."""

    def test_non_finite(self):
        for kwargs in ({"z_threshold": math.inf}, {"gap_factor": math.inf}, {"z_threshold": math.nan},
                       {"min_scale_fraction": math.nan}, {"z_threshold": True}):
            with self.assertRaises(ValueError, msg=kwargs):
                AssessmentConfig(**kwargs)


class SchemaMatchesValidationTests(unittest.TestCase):
    """P2: what the published schema accepts, the core accepts, and vice versa."""

    def test_mode_specific_arguments_are_expressed_in_the_schema(self):
        schema = tool_specs(FabricLimits())["read_observations"]["input_schema"]
        self.assertEqual(schema["if"]["properties"]["mode"], {"const": "latest"})
        try:
            import jsonschema
        except ImportError:
            self.skipTest("not_run: jsonschema not installed (installed with the MCP SDK)")
        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        session = fabric.open_session(ALICE)
        cases = [{"entity_id": FRIDGE, "mode": "latest"}, {"entity_id": FRIDGE, "mode": "latest", "limit": 1},
                 {"entity_id": FRIDGE, "mode": "latest", "cursor": "1"}, {"entity_id": FRIDGE, "mode": "history"},
                 {"entity_id": FRIDGE, "mode": "history", "limit": 1, "cursor": "3"},
                 {"entity_id": FRIDGE, "mode": "history", "limit": 0}, {"entity_id": FRIDGE},
                 {"entity_id": FRIDGE, "mode": "other"}, {"entity_id": FRIDGE, "mode": "latest", "x": 1}]
        validator = jsonschema.Draft202012Validator(schema)
        for args in cases:
            schema_ok = validator.is_valid(args)
            try:
                session.call("read_observations", args)
                core_ok = True
            except FabricError as error:
                core_ok = error.code != "invalid_arguments"
            self.assertEqual(schema_ok, core_ok, args)


class SchemaIsolationTests(unittest.TestCase):
    """P2: mutating schemas returned by tools() cannot affect validation anywhere."""

    def test_returned_schemas_are_copies(self):
        fabric, _ = world([grant("g:a", ALICE, FRIDGE), grant("g:b", BOB, FRIDGE)])
        alice, bob = fabric.open_session(ALICE), fabric.open_session(BOB)
        tools = alice.tools()
        tools["describe_entity"]["input_schema"]["properties"]["entity_id"]["type"] = "integer"
        tools["describe_entity"]["input_schema"]["properties"]["entity_id"]["maxLength"] = 1
        self.assertIn("entity", bob.call("describe_entity", {"entity_id": FRIDGE}))
        self.assertIn("entity", alice.call("describe_entity", {"entity_id": FRIDGE}))
        self.assertEqual(fabric.open_session(ALICE).tools()["describe_entity"]["input_schema"]["properties"]
                         ["entity_id"]["type"], "string")


class SignalFilterConsistencyTests(unittest.TestCase):
    """P2: with a signal filter, the overall state is derived from that signal only."""

    def test_filtered_overall_state(self):
        fabric, _ = world([grant("g:1", ALICE, CAR)])
        session = fabric.open_session(ALICE)
        self.assertEqual(session.call("explain_assessment", {"entity_id": CAR})["state"], "deviation")
        battery = session.call("explain_assessment", {"entity_id": CAR, "signal": "battery_v"})
        self.assertEqual((battery["state"], [s["signal"] for s in battery["signals"]]), ("nominal", ["battery_v"]))
        coolant = session.call("explain_assessment", {"entity_id": CAR, "signal": "coolant_temp_c"})
        self.assertEqual(coolant["state"], "deviation")


class AuditSinkFailureTests(unittest.TestCase):
    """P2: a failing audit sink yields FabricError('internal_error') only, never its own exception."""

    def test_failing_sink(self):
        def sink(_record):
            raise OSError("/var/log/secret: disk full")

        fabric = Fabric(clock=ManualClock(T0 + DAY), audit=AuditLog(sink=sink))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        session = fabric.open_session(ALICE)
        for tool, args in (("describe_entity", {"entity_id": FRIDGE}), ("list_entities", {}),
                           ("describe_entity", {"entity_id": CAR})):
            with self.assertRaises(FabricError) as ctx:
                session.call(tool, args)
            self.assertEqual(ctx.exception.code, "internal_error")  # fail closed: unaudited reads are not served
            self.assertNotIn("secret", str(ctx.exception.to_dict()))


class PolicyRewriteInPlaceTests(unittest.TestCase):
    """P1: a same-inode, same-size rewrite with the mtime restored is detected (ctime)."""

    def test_in_place_rewrite_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(__import__("json").dumps(grants_to_document(
                [grant("g:aaa", ALICE, FRIDGE, {"observe"}, T0, T0 + DAY)])))
            original = path.read_bytes()
            forged = original.replace(b"g:aaa", b"g:bbb")
            self.assertEqual(len(forged), len(original))
            real_read = os.read
            state = {"done": False}

            def rewrite_then_read(fd, n):
                if not state["done"]:
                    state["done"] = True
                    info = os.stat(path)
                    with open(path, "r+b") as handle:  # same inode, same size
                        handle.write(forged)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))  # restore mtime
                return real_read(fd, n)

            with mock.patch("pexislink.fabric.policy.os.read", side_effect=rewrite_then_read):
                with self.assertRaises(ContractError) as ctx:
                    load_policy_file(path)
            self.assertEqual(ctx.exception.code, "policy_changed")


# --- Independent owner review of 03e8406 -----------------------------------------------------------


class NumericSafetyTests(unittest.TestCase):
    """P1: finite but huge ranges must never turn an overflow into a 'nominal' verdict."""

    def test_contract_rejects_ranges_whose_arithmetic_could_overflow(self):
        from pexislink.fabric import SignalSpec
        from pexislink.fabric.contracts import MAX_SIGNAL_MAGNITUDE
        for low, high in ((-1e308, 1e308), (0.0, 1e300), (-1e16, 0.0)):
            with self.assertRaises(ContractError, msg=(low, high)):
                SignalSpec("x", "number", None, low, high)
        SignalSpec("x", "number", None, -MAX_SIGNAL_MAGNITUDE, MAX_SIGNAL_MAGNITUDE)  # the bound itself is fine

    def test_reviewer_scenario_at_the_largest_accepted_range_is_a_deviation(self):
        from pexislink.fabric import SignalSpec
        from pexislink.fabric.contracts import MAX_SIGNAL_MAGNITUDE, EntitySpec
        big = MAX_SIGNAL_MAGNITUDE
        spec = EntitySpec(entity_id="test:numeric:big", kind="test.numeric", display_name="Big range",
                          adapter="synthetic:test-v0", capabilities=frozenset({"assess"}),
                          signals=(SignalSpec("x", "number", None, -big, big),), expected_interval_s=60,
                          max_latency_s=120, stale_after_s=600)
        fabric = Fabric(clock=ManualClock(T0 + 30 * 60_000_000))
        fabric.register(spec)
        fabric.policy.add_grant(grant("g:1", ALICE, "test:numeric:big", {"assess"}))
        from pexislink.fabric.adapters._common import observation
        for i in range(30):
            t = T0 + i * 60_000_000
            fabric.ingest(observation(event_id=f"b-{i}", entity_id="test:numeric:big", adapter="synthetic:test-v0",
                                      kind="test.numeric", observed_at=t, received_at=t,
                                      payload={"x": 0.0 if i < 24 else big}, generator="tests.fabric", seed=0))
        result = fabric.open_session(ALICE).call("explain_assessment", {"entity_id": "test:numeric:big"})
        self.assertEqual((result["state"], result["signals"][0]["state"]), ("deviation", "deviation"))

    def test_overflow_behind_the_contract_abstains_instead_of_nominal(self):
        """Defence in depth: if an unsafe range ever reached the assessment, it must abstain."""
        from pexislink.fabric.assess import assess
        from pexislink.fabric.contracts import Observation
        spec = appliance.entity()
        signal = spec.signal("temperature_c")
        object.__setattr__(signal, "minimum", -1e308)  # bypass the contract on purpose
        object.__setattr__(signal, "maximum", 1e308)
        try:
            window = [Observation(f"o-{i}", FRIDGE, T0 + i * 60_000_000, T0 + i * 60_000_000, appliance.ADAPTER_ID,
                                  "appliance.telemetry", (("temperature_c", 0.0 if i < 24 else 1e308),), "observed",
                                  (), ()) for i in range(30)]
            result = assess(spec, window, T0 + 30 * 60_000_000, AssessmentConfig(), only="temperature_c")
        finally:
            object.__setattr__(signal, "minimum", -40.0)
            object.__setattr__(signal, "maximum", 40.0)
        self.assertEqual(result["signals"][0]["state"], "unknown")
        self.assertEqual(result["signals"][0]["reason"], "numerically_unsafe")
        self.assertNotEqual(result["state"], "nominal")


class HistoryCursorSchemaTests(unittest.TestCase):
    """P2: the published history cursor schema is exactly what the core accepts."""

    def test_parity_for_cursors(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("not_run: jsonschema not installed (installed with the MCP SDK)")
        specs = tool_specs(FabricLimits())
        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        session = fabric.open_session(ALICE)
        validator = jsonschema.Draft202012Validator(specs["read_observations"]["input_schema"])
        for cursor in ("1", "0", "42", "9" * 18, "9" * 19, "abc", "1a", "-1", "1.0", "", "\u00b2", "\uff11"):
            args = {"entity_id": FRIDGE, "mode": "history", "cursor": cursor}
            try:
                session.call("read_observations", args)
                core_ok = True
            except FabricError as error:
                core_ok = error.code != "invalid_arguments"
            self.assertEqual(validator.is_valid(args), core_ok, repr(cursor))
        from pexislink.fabric.contracts import MAX_ID_LENGTH
        self.assertEqual(specs["list_entities"]["input_schema"]["properties"]["cursor"]["maxLength"], MAX_ID_LENGTH)


class AuditFidelityTests(unittest.TestCase):
    """P2: 'executed' means returned to the caller; every attempt is audited without raw input."""

    def records(self, fabric):
        return [{k: r[k] for k in ("operation", "decision", "reason", "executed")} for r in fabric.audit.records()]

    def test_over_budget_result_is_not_recorded_as_executed(self):
        fabric = Fabric(clock=ManualClock(T0 + DAY), limits=FabricLimits(max_result_bytes=1))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        session = fabric.open_session(ALICE)
        for tool, args in (("describe_entity", {"entity_id": FRIDGE}), ("list_entities", {})):
            with self.assertRaises(FabricError) as ctx:
                session.call(tool, args)
            self.assertEqual(ctx.exception.code, "internal_error")
        records = self.records(fabric)
        self.assertFalse(any(r["executed"] for r in records))
        self.assertEqual([r for r in records if r["reason"] == "result_too_large"],
                         [{"operation": "describe_entity", "decision": "error", "reason": "result_too_large",
                           "executed": False},
                          {"operation": "list_entities", "decision": "error", "reason": "result_too_large",
                           "executed": False}])

    def test_rejected_attempts_are_audited_with_codes_only(self):
        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        session = fabric.open_session(ALICE)
        secret = "SECRET-MARKER-ignore-rules"
        for tool, args in ((secret, {}), ("describe_entity", {"entity_id": FRIDGE, "note": secret}),
                           ("describe_entity", {"entity_id": secret})):
            with self.assertRaises(FabricError):
                session.call(tool, args)
        records = self.records(fabric)
        self.assertEqual(records, [
            {"operation": "<unknown>", "decision": "reject", "reason": "unknown_tool", "executed": False},
            {"operation": "describe_entity", "decision": "reject", "reason": "invalid_arguments", "executed": False},
            {"operation": "describe_entity", "decision": "reject", "reason": "invalid_arguments", "executed": False},
        ])
        self.assertNotIn(secret, str(fabric.audit.records()))

    def test_successful_call_records_executed_exactly_once(self):
        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        fabric.open_session(ALICE).call("describe_entity", {"entity_id": FRIDGE})
        self.assertEqual(self.records(fabric), [
            {"operation": "describe_entity", "decision": "allow", "reason": "granted", "executed": False},
            {"operation": "describe_entity", "decision": "allow", "reason": "granted", "executed": True},
        ])


if __name__ == "__main__":
    unittest.main()
