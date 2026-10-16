"""Agent Fabric v0.1: deny-by-default policy engine, expiry, revocation, audit."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from fabric_helpers import ALICE, ALL, BOB, CAR, DAY, FRIDGE, grant

from pexislink.fabric import ContractError, Grant, ManualClock, PolicyEngine, Principal
from pexislink.fabric.adapters._common import T0
from pexislink.fabric.policy import AuditLog, grants_to_document, load_policy_file, parse_policy

DECLARED = {FRIDGE: frozenset(ALL), CAR: frozenset({"describe", "observe"})}.get


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(T0)
        self.engine = PolicyEngine(clock=self.clock)
        self.alice = Principal(ALICE)

    def decide(self, entity=FRIDGE, capability="observe", principal=None):
        return self.engine.decide(principal or self.alice, entity, capability, DECLARED)

    def test_default_is_deny(self):
        decision = self.decide()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_grant")

    def test_grant_is_per_principal_per_entity_per_capability(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        self.assertTrue(self.decide().allowed)
        self.assertEqual(self.decide(capability="assess").reason, "no_grant")
        self.assertEqual(self.decide(entity=CAR).reason, "no_grant")
        self.assertEqual(self.decide(principal=Principal(BOB)).reason, "no_grant")

    def test_decision_order_and_reasons(self):
        self.engine.add_grant(grant("g:1", ALICE, CAR, {"read_history"}))
        self.assertEqual(self.decide(capability="transmit").reason, "capability_not_supported")
        self.assertEqual(self.decide(entity="demo:x:missing").reason, "unknown_entity")
        self.assertEqual(self.decide(entity=CAR, capability="read_history").reason, "capability_not_declared")

    def test_expiry_and_not_before_use_the_injected_clock(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}, not_before=T0 + 10, expires_at=T0 + 20))
        self.assertEqual(self.decide().reason, "not_yet_valid")
        self.clock.now = T0 + 10
        self.assertTrue(self.decide().allowed)
        self.clock.now = T0 + 19
        self.assertTrue(self.decide().allowed)
        self.clock.now = T0 + 20  # expires_at is exclusive
        self.assertEqual(self.decide().reason, "expired")

    def test_revocation_is_immediate_and_ids_are_not_reusable(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        self.assertTrue(self.decide().allowed)
        self.assertTrue(self.engine.revoke("g:1"))
        self.assertEqual(self.decide().reason, "revoked")
        with self.assertRaises(ContractError):
            self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        self.assertFalse(self.engine.revoke("g:never"))  # unknown ids are pre-revoked, not an error
        with self.assertRaises(ContractError):
            self.engine.add_grant(grant("g:never", ALICE, FRIDGE, {"observe"}))

    def test_a_second_valid_grant_still_allows_after_another_is_revoked(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        self.engine.add_grant(grant("g:2", ALICE, FRIDGE, {"observe"}))
        self.engine.revoke("g:1")
        decision = self.decide()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.grant_id, "g:2")

    def test_grants_cannot_be_unbounded_or_write_like(self):
        with self.assertRaises(ContractError):
            Grant("g:1", ALICE, FRIDGE, frozenset({"observe"}), T0, T0)  # empty validity
        for capability in ("write", "transmit", "control", "*"):
            with self.assertRaises(ContractError):
                grant("g:1", ALICE, FRIDGE, {capability})
        with self.assertRaises(ContractError):
            grant("g:1", ALICE, "demo:appliance:*", {"observe"})  # no wildcards in v0.1
        engine = PolicyEngine(clock=self.clock, max_grants=2)
        engine.add_grant(grant("g:1", ALICE, FRIDGE))
        engine.add_grant(grant("g:2", ALICE, FRIDGE))
        with self.assertRaises(ContractError):
            engine.add_grant(grant("g:3", ALICE, FRIDGE))

    def test_internal_faults_fail_closed(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))

        def broken(_):
            raise RuntimeError("boom /secret/path")

        decision = self.engine.decide(self.alice, FRIDGE, "observe", broken)
        self.assertEqual((decision.allowed, decision.reason), (False, "policy_error"))
        engine = PolicyEngine(clock=lambda: 1 / 0)
        engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        self.assertEqual(engine.decide(self.alice, FRIDGE, "observe", DECLARED).reason, "policy_error")
        self.assertEqual(self.engine.decide("test:principal:alice", FRIDGE, "observe", DECLARED).reason,
                         "invalid_principal")  # a bare string is not a bound principal

    def test_concurrent_revocation_never_allows_after_it_returns(self):
        self.engine.add_grant(grant("g:1", ALICE, FRIDGE, {"observe"}))
        revoked = threading.Event()
        violations = []

        def reader():
            for _ in range(2000):
                was_revoked = revoked.is_set()
                if self.decide().allowed and was_revoked:
                    violations.append(1)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        self.engine.revoke("g:1")
        revoked.set()
        for t in threads:
            t.join()
        self.assertEqual(violations, [])
        self.assertFalse(self.decide().allowed)


class AuditTests(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        log = AuditLog()
        for i in range(5):
            log.append(at=i, principal=ALICE, operation="x", decision="allow")
        records = log.records()
        self.assertTrue(AuditLog.verify(records))
        tampered = [dict(r) for r in records]
        tampered[2]["decision"] = "deny"
        self.assertFalse(AuditLog.verify(tampered))
        self.assertFalse(AuditLog.verify(records[:2] + records[3:]))

    def test_bounded(self):
        log = AuditLog(max_records=3)
        for i in range(10):
            log.append(at=i)
        records = log.records()
        self.assertEqual([r["seq"] for r in records], [8, 9, 10])
        self.assertTrue(AuditLog.verify(records))  # a bounded suffix still verifies internally


class PolicyFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.grants = [grant("g:1", ALICE, FRIDGE, {"observe", "describe"}, T0, T0 + DAY)]
        self.path = self.root / "policy.json"
        self.path.write_text(json.dumps(grants_to_document(self.grants)))

    def tearDown(self):
        self._tmp.cleanup()

    def assertCode(self, code, path=None):
        with self.assertRaises(ContractError) as ctx:
            load_policy_file(path or self.path)
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn(str(self.root), str(ctx.exception))

    def test_round_trip(self):
        self.assertEqual(load_policy_file(self.path), self.grants)

    def test_symlink_directory_device_missing_and_oversize_are_refused(self):
        link = self.root / "link.json"
        link.symlink_to(self.path)
        self.assertCode("policy_symlink", link)
        self.assertCode("policy_not_regular_file", self.root)
        self.assertCode("policy_unreadable", self.root / "missing.json")
        self.assertCode("policy_not_regular_file", Path("/dev/null"))
        self.path.write_bytes(b" " * (64 * 1024 + 1))
        self.assertCode("policy_too_large")

    def test_invalid_documents_are_refused(self):
        for text, code in (("{", "policy_invalid_json"), ('{"schema_version": NaN}', "policy_invalid_json"),
                           ('{"a": 1, "a": 2}', "policy_duplicate_key"), (b"\xff", "policy_invalid_json"),
                           ('{"schema_version": "pexislink.policy.v0", "grants": [], "x": 1}', "unknown_field"),
                           ('{"schema_version": "pexislink.policy.v9", "grants": []}', "unsupported_schema_version")):
            self.path.write_bytes(text if isinstance(text, bytes) else text.encode())
            self.assertCode(code)

    def test_write_capabilities_in_a_file_are_refused(self):
        document = grants_to_document(self.grants)
        document["grants"][0]["capabilities"] = ["observe", "transmit"]
        with self.assertRaises(ContractError) as ctx:
            parse_policy(document)
        self.assertEqual(ctx.exception.code, "capability_not_supported")
        document["grants"][0].pop("expires_at")
        with self.assertRaises(ContractError):
            parse_policy(document)

    def test_file_replaced_between_check_and_open_is_detected(self):
        from unittest import mock
        real_open = os.open

        def swap(path, flags, *args):
            other = self.root / "other.json"
            other.write_text(self.path.read_text())
            os.replace(other, self.path)  # new inode at the same name
            return real_open(path, flags, *args)

        with mock.patch("pexislink.fabric.policy.os.open", side_effect=swap):
            self.assertCode("policy_changed")


if __name__ == "__main__":
    unittest.main()
