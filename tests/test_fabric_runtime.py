"""Agent Fabric v0.1: runtime, bounded memory, evidence states, replay, adapters."""

import hashlib
import io
import json
import socket
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fabric_helpers import ALICE, ALL, API, BOB, CAR, DAY, FRIDGE, fridge_obs, grant, world

from pexislink.fabric import Fabric, FabricError, FabricLimits, ManualClock
from pexislink.fabric.adapters import appliance, service, vehicle
from pexislink.fabric.adapters._common import T0
from pexislink.fabric.assess import AssessmentConfig, assess
from pexislink.fabric.contracts import canonical_json
from pexislink.fabric.demo import DEMO_NOW, demo_json
from pexislink.fabric.guard import EffectBlocked, no_effects


def call(session, tool, **args):
    try:
        return session.call(tool, args)
    except FabricError as error:
        return error.to_dict()


class IngestQualityTests(unittest.TestCase):
    def setUp(self):
        self.fabric = Fabric(clock=ManualClock(T0 + DAY))
        self.fabric.register(appliance.entity())

    def ingest(self, raw):
        return self.fabric.ingest(raw)

    def test_on_time_in_order_is_observed(self):
        self.assertEqual(self.ingest(fridge_obs(1)).status, "observed")

    def test_late_out_of_order_and_out_of_range(self):
        self.ingest(fridge_obs(5))
        self.assertEqual(self.ingest(fridge_obs(6, delay_s=121)).status, "late")
        older = self.fabric.store
        result = self.ingest(fridge_obs(2))
        self.assertEqual(result.status, "late")
        latest = older.page(FRIDGE, 0, 10)[0][-1][1]
        self.assertEqual(latest.flags, ("out_of_order",))
        result = self.ingest(fridge_obs(7, value=41.0))
        self.assertEqual(result.status, "unknown")
        self.assertEqual(older.page(FRIDGE, 0, 10)[0][-1][1].flags, ("out_of_range",))

    def test_rejections_are_counted_never_stored(self):
        self.ingest(fridge_obs(1))
        cases = {
            "duplicate_event": fridge_obs(1),
            "undeclared_signal": fridge_obs(2, payload={"humidity": 1.0}),
            "signal_type_mismatch": fridge_obs(3, payload={"temperature_c": "cold"}),
            "source_mismatch": fridge_obs(4, source="synthetic:vehicle-can-v0"),
            "received_in_future": fridge_obs(10 * 24 * 60),
            "unknown_entity": fridge_obs(5, entity_id="demo:appliance:other"),
            "invalid_payload_value": fridge_obs(6, payload={"temperature_c": None}),
        }
        for code, raw in cases.items():
            result = self.ingest(raw)
            self.assertEqual((result.accepted, result.code), (False, code))
        stats = self.fabric.store.stats(FRIDGE)
        self.assertEqual(stats["stored"], 1)
        self.assertEqual(set(stats["rejected"]), set(cases) - {"unknown_entity", "invalid_payload_value"})

    def test_page_offsets_are_exact_after_eviction(self):
        fabric = Fabric(clock=ManualClock(T0 + DAY), limits=FabricLimits(max_per_entity=10))
        fabric.register(appliance.entity())
        for i in range(25):
            fabric.ingest(fridge_obs(i))
        store = fabric.store
        for after, expected in ((0, list(range(16, 26))), (3, list(range(16, 26))), (15, list(range(16, 26))),
                                (16, list(range(17, 26))), (24, [25]), (25, []), (10 ** 9, [])):
            items, more = store.page(FRIDGE, after, 100)
            self.assertEqual([seq for seq, _ in items], expected, after)
            self.assertFalse(more)
        items, more = store.page(FRIDGE, 18, 3)
        self.assertEqual(([seq for seq, _ in items], more), ([19, 20, 21], True))
        self.assertEqual(store.page(FRIDGE, 22, 3)[1], False)  # exactly the last three

    def test_bounded_memory_evicts_oldest_and_reports_it(self):
        fabric = Fabric(clock=ManualClock(T0 + DAY), limits=FabricLimits(max_per_entity=10))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        for i in range(25):
            fabric.ingest(fridge_obs(i))
        stats = fabric.store.stats(FRIDGE)
        self.assertEqual((stats["stored"], stats["evicted"], stats["oldest_seq"]), (10, 15, 16))
        page = fabric.open_session(ALICE).call("read_observations", {"entity_id": FRIDGE, "mode": "history"})
        self.assertEqual(page["items"][0]["seq"], 16)
        self.assertTrue(page["retention"]["truncated_before_cursor"])
        self.assertTrue(fabric.ingest(fridge_obs(0)).accepted)  # an evicted id may re-enter: documented


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.fabric, self.clock = world([grant("g:a-fridge", ALICE, FRIDGE), grant("g:a-api", ALICE, API),
                                         grant("g:b-car", BOB, CAR, {"describe", "observe"})])
        self.alice, self.bob = self.fabric.open_session(ALICE), self.fabric.open_session(BOB)

    def test_entities_are_isolated_per_principal(self):
        self.assertEqual([e["entity_id"] for e in call(self.alice, "list_entities")["entities"]], [FRIDGE, API])
        self.assertEqual([e["entity_id"] for e in call(self.bob, "list_entities")["entities"]], [CAR])
        hidden = call(self.bob, "read_observations", entity_id=FRIDGE, mode="latest")
        missing = call(self.bob, "read_observations", entity_id="demo:appliance:nope", mode="latest")
        self.assertEqual(canonical_json(hidden), canonical_json(missing))
        self.assertEqual(hidden["error"]["code"], "not_found")

    def test_every_read_returns_only_the_requested_entity(self):
        page = call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", limit=50)
        cursor = page["next_cursor"]
        seen = 0
        while True:
            self.assertTrue(all(i["observation"]["entity_id"] == FRIDGE for i in page["items"]))
            seen += len(page["items"])
            if not page["next_cursor"]:
                break
            page = call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", cursor=page["next_cursor"])
        self.assertEqual(seen, self.fabric.store.stats(FRIDGE)["stored"])
        self.assertIsNotNone(cursor)

    def test_pagination_is_bounded_and_stable(self):
        first = call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", limit=7)
        self.assertEqual(len(first["items"]), 7)
        again = call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", limit=7)
        self.assertEqual(first, again)
        second = call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", limit=7,
                      cursor=first["next_cursor"])
        self.assertEqual(second["items"][0]["seq"], first["items"][-1]["seq"] + 1)
        for bad in ({"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": "5"}, {"cursor": "-1"},
                    {"cursor": "1e3"}, {"cursor": "9" * 19}, {"cursor": "²"}):
            self.assertEqual(call(self.alice, "read_observations", entity_id=FRIDGE, mode="history",
                                  **bad)["error"]["code"], "invalid_arguments", bad)

    def test_result_bytes_are_bounded_even_with_maximal_payloads(self):
        fabric = Fabric(clock=ManualClock(T0 + DAY), limits=FabricLimits(max_result_bytes=8 * 1024))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        for i in range(100):
            fabric.ingest(fridge_obs(i))
        page = fabric.open_session(ALICE).call("read_observations", {"entity_id": FRIDGE, "mode": "history",
                                                                      "limit": 50})
        self.assertLessEqual(len(canonical_json(page)), 8 * 1024)
        self.assertLess(len(page["items"]), 50)
        self.assertIsNotNone(page["next_cursor"])

    def test_stale_data_is_never_presented_as_current(self):
        latest = call(self.alice, "read_observations", entity_id=API, mode="latest")
        self.assertTrue(latest["freshness"]["stale"])
        self.assertFalse(latest["freshness"]["current"])
        assessment = call(self.alice, "explain_assessment", entity_id=API)
        self.assertEqual(assessment["state"], "stale")
        self.assertTrue(all(s["state"] in ("stale", "unknown") for s in assessment["signals"]))
        fresh = call(self.alice, "read_observations", entity_id=FRIDGE, mode="latest")
        self.assertTrue(fresh["freshness"]["current"])
        self.clock.advance(601)  # the same data ages past stale_after_s
        self.assertFalse(call(self.alice, "read_observations", entity_id=FRIDGE, mode="latest")["freshness"]["current"])
        self.assertEqual(call(self.alice, "explain_assessment", entity_id=FRIDGE)["state"], "stale")

    def test_missing_data_yields_insufficient_never_a_guess(self):
        fabric = Fabric(clock=ManualClock(T0 + 3_600_000_000))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        session = fabric.open_session(ALICE)
        self.assertEqual(session.call("read_observations", {"entity_id": FRIDGE, "mode": "latest"})["state"],
                         "insufficient")
        self.assertEqual(session.call("explain_assessment", {"entity_id": FRIDGE})["state"], "stale")
        for i in range(50, 60):
            fabric.ingest(fridge_obs(i))
        result = session.call("explain_assessment", {"entity_id": FRIDGE})
        self.assertEqual(result["state"], "insufficient")
        self.assertEqual(result["signals"][0]["reason"], "too_few_trustworthy_values")

    def test_unknown_observations_are_never_used_as_measurements(self):
        fabric = Fabric(clock=ManualClock(T0 + 3_600_000_000))
        fabric.register(appliance.entity())
        fabric.policy.add_grant(grant("g:1", ALICE, FRIDGE))
        for i in range(30, 60):
            fabric.ingest(fridge_obs(i, value=4.0 if i < 55 else 39.9))
        for i in range(55, 60):  # would be a large deviation if they were trusted
            raw = fridge_obs(1000 + i, value=39.9)
            raw["observed_at"] = raw["received_at"] = fridge_obs(i)["observed_at"].replace(":00Z", ":30Z")
            raw["quality"]["status"] = "unknown"
            fabric.ingest(raw)
        result = fabric.open_session(ALICE).call("explain_assessment", {"entity_id": FRIDGE})
        self.assertEqual(result["inputs"]["status_counts"]["unknown"], 5)
        self.assertEqual(result["signals"][0]["evidence"]["trustworthy_values"], 30)

    def test_capability_matrix_separates_declared_available_authorized(self):
        rows = {r["capability"]: r for r in call(self.bob, "list_capabilities", entity_id=CAR)["capabilities"]}
        self.assertEqual(rows["assess"], {"capability": "assess", "declared": True, "available": True,
                                          "authorized": False, "reason": "no_grant"})
        self.assertTrue(rows["observe"]["authorized"])
        self.assertEqual(call(self.bob, "explain_assessment", entity_id=CAR)["error"]["code"], "access_denied")

    def test_audit_records_decisions_and_executions(self):
        call(self.bob, "explain_assessment", entity_id=CAR)
        call(self.bob, "read_observations", entity_id=CAR, mode="latest")
        records = self.fabric.audit.records()
        self.assertEqual([(r["operation"], r["decision"], r["executed"]) for r in records[-3:]],
                         [("explain_assessment", "deny", False), ("read_observations", "allow", False),
                          ("read_observations", "allow", True)])
        self.assertTrue(self.fabric.audit.verify(records))
        text = canonical_json(records)
        self.assertNotIn("temperature_c", text)  # identifiers and codes only, never values

    def test_sessions_share_no_state(self):
        before = canonical_json(call(self.bob, "list_entities"))
        for _ in range(3):
            call(self.alice, "read_observations", entity_id=FRIDGE, mode="history", limit=3)
            call(self.alice, "explain_assessment", entity_id=FRIDGE)
        self.assertEqual(canonical_json(call(self.bob, "list_entities")), before)
        self.assertFalse(hasattr(self.alice, "__dict__") and any(
            k not in ("_fabric", "_principal", "_specs") for k in vars(self.alice)))


class AssessmentTests(unittest.TestCase):
    def test_fridge_drift_is_detected_for_every_seed_and_absent_before_it(self):
        for seed in range(1, 21):
            fabric = Fabric(clock=ManualClock(DEMO_NOW))
            fabric.register(appliance.entity())
            fabric.ingest_all(appliance.generate(seed=seed))
            window = fabric.store.window(FRIDGE, 120)
            after = assess(appliance.entity(), window, DEMO_NOW, AssessmentConfig())["signals"][0]
            cut = DEMO_NOW - 26 * 60_000_000
            before = assess(appliance.entity(), [o for o in window if o.observed_at < cut], cut,
                            AssessmentConfig())["signals"][0]
            self.assertEqual((after["state"], before["state"]), ("deviation", "nominal"), seed)

    def test_vehicle_coolant_rise_is_a_deviation_and_unknown_frames_are_not_data(self):
        fabric, _ = world([grant("g:1", ALICE, CAR)])
        result = fabric.open_session(ALICE).call("explain_assessment", {"entity_id": CAR})
        states = {s["signal"]: s["state"] for s in result["signals"]}
        self.assertEqual(states["coolant_temp_c"], "deviation")
        self.assertEqual(states["battery_v"], "nominal")  # the 655.35 V frame is unknown, not a measurement
        self.assertEqual(result["confidence"], "low")
        self.assertIn("not a diagnosis", result["notice"])

    def test_config_is_validated(self):
        for kwargs in ({"recent": 1}, {"min_points": 3, "recent": 6}, {"window": 5000}, {"z_threshold": 0}):
            with self.assertRaises(ValueError):
                AssessmentConfig(**kwargs)


class AdapterTests(unittest.TestCase):
    def test_vehicle_unknown_and_truncated_frames_carry_no_payload(self):
        raws = vehicle.generate()
        unknown = [r for r in raws if r["kind"] == "vehicle.can_unknown_frame"]
        truncated = [r for r in raws if "length_mismatch" in r["quality"]["flags"]]
        self.assertTrue(unknown and truncated)
        self.assertTrue(all(r["payload"] == {} and r["quality"]["status"] == "unknown" for r in unknown + truncated))

    def test_vehicle_decoder_matches_hand_computed_values(self):
        engine = vehicle.SIGNAL_TABLE[(0x101, False)]
        data = bytes([0x82, 0x40, 0x1F, 0, 0, 0, 0, 0])  # 0x82 - 40 = 90 degC; 0x1F40 * 0.25 = 2000 rpm
        self.assertEqual([vehicle.decode_signal(s, data) for s in engine.signals], [90.0, 2000.0])
        power = vehicle.SIGNAL_TABLE[(0x102, False)]
        self.assertEqual(vehicle.decode_signal(power.signals[1], bytes([0, 0, 0xF6, 0])), -10.0)  # signed
        for values in ({"coolant_temp_c": 91, "engine_speed_rpm": 1234.5}, {"coolant_temp_c": -40,
                                                                          "engine_speed_rpm": 0}):
            data = vehicle.encode(engine, values)
            self.assertEqual({s.name: vehicle.decode_signal(s, data) for s in engine.signals}, values)

    def test_identifiers_are_synthetic(self):
        for module in (appliance, vehicle, service):
            spec = module.entity()
            self.assertTrue(spec.entity_id.startswith("demo:"))
            self.assertTrue(spec.adapter.startswith("synthetic:"))

    def test_core_contains_no_domain_vocabulary(self):
        core = Path(__file__).resolve().parents[1] / "src" / "pexislink" / "fabric"
        for name in ("contracts.py", "policy.py", "store.py", "assess.py", "runtime.py", "guard.py"):
            text = (core / name).read_text(encoding="utf-8").lower()
            for word in ("fridge", "refrigerator", "vehicle", "can frame", "dbc", "coolant", "rpm", "compressor", "checkout"):
                self.assertFalse(word in text, f"{name}: {word}")


class ReplayTests(unittest.TestCase):
    def test_replay_is_byte_identical(self):
        def digest():
            fabric, _ = world([grant("g:1", ALICE, e) for e in ()] + [grant("g:f", ALICE, FRIDGE),
                                                                     grant("g:c", ALICE, CAR),
                                                                     grant("g:s", ALICE, API)])
            session = fabric.open_session(ALICE)
            parts = []
            for entity in (FRIDGE, CAR, API):
                cursor = None
                while True:
                    args = {"entity_id": entity, "mode": "history"} | ({"cursor": cursor} if cursor else {})
                    page = session.call("read_observations", args)
                    parts.append(canonical_json(page))
                    cursor = page.get("next_cursor")
                    if not cursor:
                        break
                parts.append(canonical_json(session.call("explain_assessment", {"entity_id": entity})))
            return hashlib.sha256("\n".join(parts).encode()).hexdigest()

        self.assertEqual(digest(), digest())
        self.assertEqual(demo_json(), demo_json())

    def test_arrival_order_shuffle_keeps_assessment_inputs_identical(self):
        raws = appliance.generate(count=120)
        now = T0 + 2 * 3600 * 1_000_000
        a = Fabric(clock=ManualClock(now))
        b = Fabric(clock=ManualClock(now))
        for fabric, order in ((a, raws), (b, list(reversed(raws)))):
            fabric.register(appliance.entity())
            fabric.ingest_all(order)
        first = assess(appliance.entity(), a.store.window(FRIDGE, 120), now, AssessmentConfig())
        second = assess(appliance.entity(), b.store.window(FRIDGE, 120), now, AssessmentConfig())
        self.assertEqual(first["signals"][0]["evidence"], second["signals"][0]["evidence"])
        self.assertEqual(first["signals"][0]["state"], "deviation")  # the drift scales with count
        self.assertGreater(second["inputs"]["status_counts"]["late"], 100)  # reversed delivery is flagged
        self.assertEqual(second["confidence"], "low")


class GuardTests(unittest.TestCase):
    def test_effects_are_blocked_inside_the_guard(self):
        attempts = [
            lambda: socket.socket(),
            lambda: subprocess.run(["true"], check=False),
            lambda: open(Path(__file__).with_name("never-written.txt"), "w"),
            lambda: open("/dev/null", "rb"),
            lambda: __import__("os").remove("/nonexistent-pexislink"),
        ]
        for attempt in attempts:
            with self.assertRaises(EffectBlocked), no_effects():
                attempt()
        self.assertFalse(Path(__file__).with_name("never-written.txt").exists())
        socket.socket().close()  # outside the guard nothing is affected

    def test_session_calls_run_inside_the_guard(self):
        from unittest import mock

        fabric, _ = world([grant("g:1", ALICE, FRIDGE)])
        session = fabric.open_session(ALICE)
        with mock.patch("pexislink.fabric.runtime.assess", side_effect=lambda *a: socket.socket()):
            self.assertEqual(call(session, "explain_assessment", entity_id=FRIDGE),
                             {"error": {"code": "internal_error", "message": "The request could not be completed."}})


class DemoCliTests(unittest.TestCase):
    def test_demo_command_is_deterministic(self):
        from pexislink.cli import main

        outputs = []
        for _ in range(2):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(["fabric", "demo"]), 0)
            outputs.append(buffer.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        demo = json.loads(outputs[0])
        self.assertEqual(demo["guest.vehicle_hidden"], demo["guest.nonexistent"])
        self.assertEqual(demo["after_revocation.analyst_fridge"]["error"]["reason"], "revoked")
        self.assertTrue(demo["audit"]["chain_valid"])


if __name__ == "__main__":
    unittest.main()
