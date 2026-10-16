"""PR #11 final correction pass: invariants stated by the owner's correction brief.

N1  No assessment returns 'nominal' when its conclusion depends on an invalid or non-finite computation.
S1  A structurally schema-valid argument is never rejected by the core's structural validation.
A1  The audit never records as completed an operation that failed final result validation; every attempt
    leaves a bounded, sanitized record; the hash chain stays valid across failures.
"""

import json
import math
import random
import unittest

from fabric_helpers import ALICE, BOB, CAR, DAY, FRIDGE, fridge_obs, grant, world

from pexislink.fabric import ContractError, EntitySpec, Fabric, FabricError, FabricLimits, ManualClock, SignalSpec
from pexislink.fabric.adapters import appliance
from pexislink.fabric.adapters._common import T0, observation
from pexislink.fabric.assess import AssessmentConfig, assess
from pexislink.fabric.contracts import MAX_SIGNAL_MAGNITUDE, Observation
from pexislink.fabric.policy import AuditLog
from pexislink.fabric.runtime import tool_specs

try:
    import jsonschema
except ImportError:  # installed with the optional MCP SDK
    jsonschema = None

ENTITY = "test:numeric:x"
ADAPTER = "synthetic:test-v0"


def numeric_fabric(low, high, values, now_offset=None, extra_signals=()):
    spec = EntitySpec(entity_id=ENTITY, kind="test.numeric", display_name="Numeric test", adapter=ADAPTER,
                      capabilities=frozenset({"assess", "describe", "read_history"}),
                      signals=(SignalSpec("x", "number", None, low, high), *extra_signals),
                      expected_interval_s=60, max_latency_s=120, stale_after_s=600)
    fabric = Fabric(clock=ManualClock(T0 + (now_offset or len(values)) * 60_000_000))
    fabric.register(spec)
    fabric.policy.add_grant(grant("g:1", ALICE, ENTITY, {"assess", "describe", "read_history"}))
    for i, value in enumerate(values):
        t = T0 + i * 60_000_000
        result = fabric.ingest(observation(event_id=f"n-{i}", entity_id=ENTITY, adapter=ADAPTER, kind="test.numeric",
                                           observed_at=t, received_at=t, payload=value if isinstance(value, dict)
                                           else {"x": value}, generator="tests.fabric", seed=0))
        assert result.accepted, result
    return fabric, fabric.open_session(ALICE)


def window_of(values, name="x"):
    return [Observation(f"o-{i}", ENTITY, T0 + i * 60_000_000, T0 + i * 60_000_000, ADAPTER, "test.numeric",
                        ((name, v),), "observed", (), ()) for i, v in enumerate(values)]


def bypass_spec(low, high, extra=()):
    """An EntitySpec whose range bypasses the contract (defence-in-depth tests only)."""
    signal = SignalSpec("x", "number", None, -1.0, 1.0)
    object.__setattr__(signal, "minimum", low)
    object.__setattr__(signal, "maximum", high)
    return EntitySpec(entity_id=ENTITY, kind="test.numeric", display_name="Bypass", adapter=ADAPTER,
                      capabilities=frozenset({"assess"}), signals=(signal, *extra), expected_interval_s=60,
                      max_latency_s=120, stale_after_s=600)


class NumericIntegrityTests(unittest.TestCase):
    """N1."""

    def test_extremes_at_the_bound_both_signs_even_and_odd_windows(self):
        big = MAX_SIGNAL_MAGNITUDE
        for sign in (1, -1):
            for n_baseline in (24, 25):  # even and odd baseline medians
                values = [0.0] * n_baseline + [sign * big] * 6
                _, session = numeric_fabric(-big, big, values)
                result = session.call("explain_assessment", {"entity_id": ENTITY})
                signal = result["signals"][0]
                self.assertEqual(signal["state"], "deviation", (sign, n_baseline))
                self.assertTrue(all(math.isfinite(v) for v in signal["evidence"].values()
                                    if isinstance(v, float)))
                self.assertEqual(signal["reason"], "recent_median_above_baseline" if sign > 0
                                 else "recent_median_below_baseline")

    def test_alternating_extremes_produce_finite_mad_and_scale(self):
        big = MAX_SIGNAL_MAGNITUDE
        values = [big if i % 2 else -big for i in range(24)] + [big] * 6
        _, session = numeric_fabric(-big, big, values)
        evidence = session.call("explain_assessment", {"entity_id": ENTITY})["signals"][0]["evidence"]
        for key in ("baseline_median", "baseline_mad", "scale", "recent_median", "robust_z"):
            self.assertTrue(math.isfinite(evidence[key]), key)

    def test_zero_and_near_zero_values_keep_existing_behaviour(self):
        values = [0.0] * 24 + [1e-12] * 6  # below the scale floor: nominal, by the documented method
        _, session = numeric_fabric(-1.0, 1.0, values)
        signal = session.call("explain_assessment", {"entity_id": ENTITY})["signals"][0]
        self.assertEqual((signal["state"], signal["evidence"]["scale_floor_used"]), ("nominal", True))
        values = [0.0] * 24 + [0.5] * 6
        _, session = numeric_fabric(-1.0, 1.0, values)
        self.assertEqual(session.call("explain_assessment", {"entity_id": ENTITY})["signals"][0]["state"],
                         "deviation")

    def test_underflowing_scale_floor_abstains_instead_of_dividing_by_zero(self):
        tiny = 5e-324  # the smallest positive double: range * 0.001 underflows to 0.0
        _, session = numeric_fabric(0.0, tiny, [0.0] * 30)
        result = session.call("explain_assessment", {"entity_id": ENTITY})
        self.assertEqual((result["signals"][0]["state"], result["signals"][0]["reason"]),
                         ("unknown", "numerically_unsafe"))
        self.assertNotEqual(result["state"], "nominal")

    def test_non_finite_z_abstains(self):
        spec = bypass_spec(-1e308, 1e308)
        for values in ([0.0] * 24 + [1e308] * 6, [-1e308] * 24 + [1e308] * 6, [1e308, -1e308] * 12 + [0.0] * 6):
            result = assess(spec, window_of(values), T0 + 30 * 60_000_000, AssessmentConfig())
            self.assertEqual(result["signals"][0]["reason"], "numerically_unsafe", values[:2])
            self.assertNotEqual(result["state"], "nominal")
            json.dumps(result, allow_nan=False)  # no non-finite number ever leaves the assessment

    def test_overall_state_is_never_nominal_when_a_signal_was_numerically_unsafe(self):
        spec = bypass_spec(-1e308, 1e308, extra=(SignalSpec("y", "number", None, -10.0, 10.0),))
        window = [Observation(f"o-{i}", ENTITY, T0 + i * 60_000_000, T0 + i * 60_000_000, ADAPTER, "test.numeric",
                              (("x", 0.0 if i < 24 else 1e308), ("y", 1.0)), "observed", (), ()) for i in range(30)]
        result = assess(spec, window, T0 + 30 * 60_000_000, AssessmentConfig())
        states = {s["signal"]: s["state"] for s in result["signals"]}
        self.assertEqual(states, {"x": "unknown", "y": "nominal"})
        self.assertNotEqual(result["state"], "nominal")
        self.assertEqual(result["confidence"], "low")

    def test_contract_rejects_out_of_bound_and_non_finite_ranges(self):
        for low, high in ((-1e308, 1e308), (-math.inf, 0.0), (0.0, math.nan), (-2e15, 0.0), (0.0, 1e16)):
            with self.assertRaises(ContractError):
                SignalSpec("x", "number", None, low, high)


def _structural_rejection(error):
    """Structural = invalid_arguments without a semantic reason."""
    return error.code == "invalid_arguments" and error.reason is None


@unittest.skipIf(jsonschema is None, "not_run: jsonschema not installed (installed with the MCP SDK)")
class SchemaParityTests(unittest.TestCase):
    """S1."""

    def setUp(self):
        self.fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        self.session = self.fabric.open_session(ALICE)
        self.validators = {name: jsonschema.Draft202012Validator(spec["input_schema"])
                           for name, spec in self.session.tools().items()}
        for spec in self.session.tools().values():
            jsonschema.Draft202012Validator.check_schema(spec["input_schema"])

    def agree(self, tool, args):
        schema_ok = self.validators[tool].is_valid(args)
        try:
            self.session.call(tool, args)
            core_ok = True
        except FabricError as error:
            core_ok = not _structural_rejection(error)
        self.assertEqual(schema_ok, core_ok, (tool, args))

    def test_history_cursor_cases_including_a_real_page_cursor(self):
        page = self.session.call("read_observations", {"entity_id": FRIDGE, "mode": "history", "limit": 5})
        real = page["next_cursor"]
        self.assertTrue(real.isdigit())
        for cursor in (real, "1", "0", "9" * 18, "9" * 19, "abc", "1a", "-1", "+1", "1.0", "", " 1", "²",
                       "１", "١"):
            self.agree("read_observations", {"entity_id": FRIDGE, "mode": "history", "cursor": cursor})
        follow = self.session.call("read_observations", {"entity_id": FRIDGE, "mode": "history", "cursor": real,
                                                         "limit": 5})
        self.assertEqual(follow["items"][0]["seq"], int(real) + 1)

    def test_mode_specific_arguments(self):
        for args in ({"entity_id": FRIDGE, "mode": "latest"}, {"entity_id": FRIDGE, "mode": "latest", "limit": 1},
                     {"entity_id": FRIDGE, "mode": "latest", "cursor": "1"},
                     {"entity_id": FRIDGE, "mode": "history", "limit": 50, "cursor": "3"},
                     {"entity_id": FRIDGE, "mode": "history", "limit": 51}):
            self.agree("read_observations", args)

    def test_generative_parity_over_every_tool(self):
        rng = random.Random(20261016)
        values = [None, True, 0, 1, 18, 50, 51, -1, 1.0, "", "1", "9" * 19, "abc", "latest", "history", FRIDGE, CAR,
                  "demo:x", "Demo:x", "a" * 161, "temperature_c", "Temperature", "nope_signal", "²", [], {}]
        keys = ["entity_id", "mode", "cursor", "limit", "signal", "principal"]
        for _ in range(4000):
            tool = rng.choice(sorted(self.validators))
            args = {k: rng.choice(values) for k in rng.sample(keys, rng.randrange(0, 4))}
            self.agree(tool, args)

    def test_unknown_signal_is_a_semantic_rejection_with_a_reason(self):
        with self.assertRaises(FabricError) as ctx:
            self.session.call("explain_assessment", {"entity_id": FRIDGE, "signal": "nope_signal"})
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_arguments", "unknown_signal"))


class AuditSemanticsTests(unittest.TestCase):
    """A1."""

    FIELDS = ("event", "operation", "decision", "reason", "executed")

    def records(self, fabric):
        return [tuple(r.get(k) for k in self.FIELDS) for r in fabric.audit.records()]

    def test_every_outcome_maps_to_one_explicit_event(self):
        fabric, clock = world([grant("g:a", ALICE, FRIDGE), grant("g:short", ALICE, CAR, {"describe"},
                                                                 expires_at=T0 + 4 * 3600 * 1_000_000 + 1)])
        session = fabric.open_session(ALICE)

        def attempt(tool, args):
            try:
                session.call(tool, args)
            except FabricError:
                pass

        attempt("describe_entity", {"entity_id": FRIDGE})                    # 1 valid
        attempt("nope", {})                                                  # 2 unknown tool
        attempt("describe_entity", {"entity_id": FRIDGE, "x": 1})            # 3 invalid arguments
        attempt("explain_assessment", {"entity_id": CAR})                    # 4 denied (no assess grant)
        clock.advance(1)
        attempt("describe_entity", {"entity_id": CAR})                       # 5 expired
        fabric.policy.revoke("g:a")
        attempt("describe_entity", {"entity_id": FRIDGE})                    # 6 revoked
        self.assertEqual(self.records(fabric), [
            ("authorization", "describe_entity", "allow", "granted", False),
            ("completed", "describe_entity", "allow", "granted", True),
            ("rejected", "<unknown>", "reject", "unknown_tool", False),
            ("rejected", "describe_entity", "reject", "invalid_arguments", False),
            ("authorization", "explain_assessment", "deny", "no_grant", False),
            ("authorization", "describe_entity", "deny", "expired", False),
            ("authorization", "describe_entity", "deny", "revoked", False),
        ])
        self.assertTrue(AuditLog.verify(fabric.audit.records()))

    def test_over_budget_and_internal_errors_are_failed_events_and_the_chain_holds(self):
        from unittest import mock
        fabric = Fabric(clock=ManualClock(T0 + DAY), limits=FabricLimits(max_result_bytes=1))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        session = fabric.open_session(ALICE)
        with self.assertRaises(FabricError):
            session.call("describe_entity", {"entity_id": FRIDGE})
        with mock.patch("pexislink.fabric.runtime.assess", side_effect=RuntimeError("/secret/path boom")):
            with self.assertRaises(FabricError) as ctx:
                session.call("explain_assessment", {"entity_id": FRIDGE})
        self.assertEqual(ctx.exception.to_dict()["error"]["code"], "internal_error")
        records = self.records(fabric)
        self.assertIn(("failed", "describe_entity", "error", "result_too_large", False), records)
        self.assertIn(("failed", "explain_assessment", "error", "internal_error", False), records)
        self.assertFalse(any(r[-1] for r in records))
        self.assertNotIn("secret", json.dumps(fabric.audit.records()))
        self.assertTrue(AuditLog.verify(fabric.audit.records()))

    def test_failing_sink_fails_closed_and_the_in_memory_chain_stays_valid(self):
        calls = []

        def sink(record):
            calls.append(record["seq"])
            raise OSError("disk full")

        fabric = Fabric(clock=ManualClock(T0 + DAY), audit=AuditLog(sink=sink))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        session = fabric.open_session(ALICE)
        for tool, args in (("describe_entity", {"entity_id": FRIDGE}), ("nope", {}),
                           ("describe_entity", {"entity_id": FRIDGE, "x": 1})):
            with self.assertRaises(FabricError) as ctx:
                session.call(tool, args)
            self.assertEqual(ctx.exception.code, "internal_error")
        self.assertTrue(calls)
        self.assertTrue(AuditLog.verify(fabric.audit.records()))

    def test_audit_is_bounded_under_a_flood_of_rejections(self):
        fabric = Fabric(clock=ManualClock(T0), limits=FabricLimits(max_audit=50))
        session = fabric.open_session(BOB)
        for i in range(500):
            try:
                session.call(f"tool-{i}", {"x": "y" * 1000})
            except FabricError:
                pass
        records = fabric.audit.records()
        self.assertEqual(len(records), 50)
        self.assertLess(len(json.dumps(records)), 50 * 400)  # bounded size per record: no raw input stored
        self.assertTrue(AuditLog.verify(records))

    def test_no_request_can_reach_the_audit_log(self):
        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        session = fabric.open_session(ALICE)
        self.assertNotIn("audit", " ".join(session.tools()))
        for name in ("_fabric", "audit"):
            self.assertFalse(any(name in str(v.get("input_schema")) for v in session.tools().values()))


if __name__ == "__main__":
    unittest.main()
