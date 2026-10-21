import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from petron.mission import MissionSpec, MissionState
from petron.redact import REDACTED, redact_text
from petron.store import Store, StoreError

from support import E001, e001_spec

S = MissionState


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-store-"))
        self.store = Store(self.tmp / "db.sqlite3")
        self.spec = MissionSpec.parse(e001_spec(E001 / "project"))
        self.store.create_mission(self.spec)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TransitionTests(StoreCase):
    def test_database_file_is_private(self):
        self.assertEqual(os.stat(self.tmp / "db.sqlite3").st_mode & 0o777, 0o600)

    def test_compare_and_set_conflict(self):
        self.store.transition("petron-e001", S.CREATED, S.PLANNING)
        with self.assertRaises(StoreError) as caught:
            self.store.transition("petron-e001", S.CREATED, S.PLANNING)
        self.assertEqual(caught.exception.code, "state_conflict")

    def test_sql_trigger_blocks_illegal_transition_even_from_raw_sql(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.raw().execute("UPDATE missions SET state = 'running' WHERE mission_id = 'petron-e001'")
        self.assertEqual(self.store.state("petron-e001"), S.CREATED)

    def test_spec_is_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.raw().execute("UPDATE missions SET spec_json = '{}' WHERE mission_id = 'petron-e001'")

    def test_plan_written_once(self):
        self.store.transition("petron-e001", S.CREATED, S.PLANNING)
        plan = {"schema": "petron.plan.v0", "rationale": "", "steps": [{"tool": "list_files", "args": {}}]}
        self.store.set_plan("petron-e001", plan, "a" * 64, {"provider": "test"})
        with self.assertRaises(StoreError):
            self.store.set_plan("petron-e001", plan, "b" * 64, {"provider": "test"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.raw().execute("UPDATE missions SET plan_sha256 = 'x' WHERE mission_id = 'petron-e001'")

    def test_concurrent_writers_only_one_wins(self):
        results = []
        barrier = threading.Barrier(4)

        def attempt():
            store = Store(self.tmp / "db.sqlite3")  # one connection per writer, as separate processes would
            barrier.wait()
            try:
                store.transition("petron-e001", S.CREATED, S.PLANNING)
                results.append("won")
            except StoreError as error:
                results.append(error.code)
            finally:
                store.close()

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["state_conflict"] * 3 + ["won"])
        self.assertEqual(self.store.state("petron-e001"), S.PLANNING)


class ApprovalTests(StoreCase):
    def test_approval_requires_awaiting_state_and_matching_hash(self):
        with self.assertRaises(StoreError) as caught:
            self.store.record_approval("petron-e001", "a" * 64, "op", "cli")
        self.assertEqual(caught.exception.code, "not_awaiting_approval")
        self.store.transition("petron-e001", S.CREATED, S.PLANNING)
        plan = {"schema": "petron.plan.v0", "rationale": "", "steps": [{"tool": "list_files", "args": {}}]}
        self.store.set_plan("petron-e001", plan, "a" * 64, {})
        self.store.transition("petron-e001", S.PLANNING, S.AWAITING_APPROVAL)
        with self.assertRaises(StoreError) as caught:
            self.store.record_approval("petron-e001", "b" * 64, "op", "cli")
        self.assertEqual(caught.exception.code, "plan_hash_mismatch")
        self.store.record_approval("petron-e001", "a" * 64, "op", "cli")
        self.assertIsNotNone(self.store.approval_for("petron-e001", "a" * 64))
        for statement in ("UPDATE approvals SET approver = 'mallory'", "DELETE FROM approvals"):
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                self.store.raw().execute(statement)


class EventLogTests(StoreCase):
    def test_events_are_append_only(self):
        self.store.put_artifact("petron-e001", "note.txt", "evidence")  # row-level triggers need rows
        for statement in ("UPDATE events SET kind = 'x'", "DELETE FROM events", "UPDATE artifacts SET content = ''",
                          "DELETE FROM artifacts", "DELETE FROM missions"):
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                self.store.raw().execute(statement)

    def test_chain_detects_tampering_after_guards_are_removed(self):
        self.store.append_event("petron-e001", "note", {"n": 1})
        self.store.append_event("petron-e001", "note", {"n": 2})
        self.assertEqual(self.store.verify_chain(), (True, None))
        raw = self.store.raw()
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json = '{\"n\":99}' WHERE seq = 2")
        ok, bad = self.store.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 2)

    def test_chain_detects_deletion(self):
        self.store.append_event(None, "note", {"n": 1})
        self.store.append_event(None, "note", {"n": 2})
        raw = self.store.raw()
        raw.execute("DROP TRIGGER events_no_delete")
        raw.execute("DELETE FROM events WHERE seq = 2")
        self.assertFalse(self.store.verify_chain()[0])

    def test_secrets_are_redacted_before_persisting(self):
        self.store.append_event("petron-e001", "note", {"text": "key sk-ant-api03-ABCDEFGHIJKLMNOP and token=hunter2hunter2"})
        payload = self.store.events("petron-e001")[-1]["payload_json"]
        self.assertNotIn("sk-ant-api03", payload)
        self.assertNotIn("hunter2hunter2", payload)
        self.assertIn(REDACTED, payload)


class RedactionTests(unittest.TestCase):
    def test_env_secret_values_are_redacted(self):
        os.environ["PETRON_TEST_API_KEY"] = "zz-very-secret-value-123"
        try:
            self.assertNotIn("zz-very-secret-value-123", redact_text("value=zz-very-secret-value-123 here"))
        finally:
            del os.environ["PETRON_TEST_API_KEY"]

    def test_private_key_block_redacted(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(redact_text(text), REDACTED)


if __name__ == "__main__":
    unittest.main()
