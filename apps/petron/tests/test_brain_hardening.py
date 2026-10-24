"""PR #25 review round: interrupted runners, HTTP refusals, draft lifecycle, bridge ownership.

Each class pins one reviewed defect. The assertions describe the property (what the operator and
the Brain may rely on), not the shape of the fix.
"""

import datetime as dt
import http.client
import json
import secrets
import socket
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from petron import brain_bridge
from petron.brain_bridge import BrainServer, EventReader, snapshot
from petron.brain_commands import BrainCommands
from petron.engine import Engine
from petron.mission import MissionState
from petron.providers.base import ProviderError
from petron.store import Store, _LOCAL_LEASES

from support import E002FakeSandbox, e002_spec
from test_brain_bridge import BridgeCase
from test_brain_commands import CommandCase

S = MissionState
UTC = dt.timezone.utc


def to_state(store, mission_id, target):
    path = ["planning", "awaiting_approval", "running", "verifying"]
    current = "created"
    for step in path:
        store.transition(mission_id, MissionState(current), MissionState(step))
        current = step
        if step == target:
            return


DEAD_PID = 2 ** 22 + 12345


class InterruptedRunnerTests(BridgeCase):
    """FIX 03: a runner that dies while running or verifying is reported as interrupted."""

    def started(self, state):
        self.store.create_mission(self.spec)
        to_state(self.store, "petron-e002", state)
        return "petron-e002"

    def dead_lease(self, mission_id):
        self.store.append_event(mission_id, "run.lease", {"pid": DEAD_PID, "host": socket.gethostname(),
                                                         "token": "dead"})

    def test_a_dead_runner_is_interrupted_in_both_active_states(self):
        for state in ("running", "verifying"):
            with self.subTest(state=state):
                self.setUp()
                self.addCleanup(self.tearDown)
                mission_id = self.started(state)
                self.dead_lease(mission_id)
                current = snapshot(self.reader())
                self.assertEqual(current["mission_state"], state)
                self.assertTrue(current["interrupted"], current)
                self.assertEqual(current["state"], "idle")          # no activity is invented
                self.assertIn("interrompida", current["label"])
                self.assertNotIn(current["state"], ("success", "verifying", "working"))

    def test_a_live_runner_keeps_the_state_it_reports(self):
        mission_id = self.started("verifying")
        self.store.acquire_lease(mission_id)                         # this process holds a registered lease
        current = snapshot(self.reader())
        self.assertFalse(current["interrupted"])
        self.assertEqual(current["state"], "verifying")

    def test_a_released_lease_on_a_non_terminal_mission_is_interrupted_not_active(self):
        for state in ("running", "verifying"):
            with self.subTest(state=state):
                self.setUp()
                self.addCleanup(self.tearDown)
                mission_id = self.started(state)
                holder = self.store.acquire_lease(mission_id)
                self.store.release_lease(mission_id, holder["token"])
                self.assertTrue(snapshot(self.reader())["interrupted"])

    def test_an_agent_that_never_took_a_lease_is_interrupted_only_after_the_startup_grace(self):
        self.store.create_mission(self.spec)
        old = Store(self.home / "petron.sqlite3",
                    clock=lambda: (dt.datetime.now(UTC) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        to_state(old, "petron-e002", "running")
        old.close()
        self.assertTrue(snapshot(self.reader())["interrupted"], "killed before the lease: shown as active forever")

    def test_a_fresh_start_without_a_lease_yet_is_not_interrupted(self):
        self.started("running")
        current = snapshot(self.reader())
        self.assertFalse(current["interrupted"])
        self.assertEqual(current["state"], "working")

    def test_a_mission_that_ended_is_never_reported_as_interrupted(self):
        mission_id = self.started("verifying")
        self.dead_lease(mission_id)
        self.store.transition(mission_id, S.VERIFYING, S.COMPLETED)
        self.assertFalse(snapshot(self.reader())["interrupted"])


class StreamTests(BridgeCase):
    """FIX 03 over SSE: heartbeat while connected, snapshot after reconnecting."""

    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(brain_bridge, "POLL_S", 0.05), mock.patch.object(brain_bridge, "HEARTBEAT_S", 0.2)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.server = BrainServer(self.home, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.server.port}"})
        return conn, conn.getresponse()

    def next_event(self, response, wanted, limit=40):
        name = None
        for _ in range(limit * 6):
            line = response.readline().decode()
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: ") and name == wanted:
                return json.loads(line[6:])
            elif not line:
                break
        self.fail(f"no {wanted} event")

    def test_a_runner_that_dies_mid_stream_is_announced_in_both_states(self):
        for state in ("running", "verifying"):
            with self.subTest(state=state):
                self.store.create_mission(self.spec)
                to_state(self.store, "petron-e002", state)
                holder = self.store.acquire_lease("petron-e002")
                conn, response = self.stream()
                first = self.next_event(response, "snapshot")
                self.assertFalse(first["interrupted"])
                _LOCAL_LEASES.discard(("petron-e002", holder["token"]))     # the runner unwound without releasing
                status = self.next_event(response, "status")
                self.assertTrue(status["interrupted"])
                self.assertEqual(status["state"], "idle")
                conn.close()
                # a page connecting afterwards sees the same truth at once, not a fake "verifying"
                conn, response = self.stream()
                again = self.next_event(response, "snapshot")
                self.assertTrue(again["interrupted"])
                conn.close()
                self.tearDown()
                self.setUp()


class RawHttp:
    """One TCP connection, bytes in and out: proves how a refusal leaves the connection."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)

    def exchange(self, data: bytes) -> bytes:
        self.sock.sendall(data)
        chunks = []
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        self.sock.close()
        return b"".join(chunks)


class RefusalConnectionTests(CommandCase):
    """FIX 04: a refused POST never lets its unread body be parsed as the next request."""

    def setUp(self):
        super().setUp()
        self.secret = secrets.token_urlsafe(32)
        self.server = BrainServer(self.home, "127.0.0.1", 0, self.commands, self.secret)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.port = self.server.port

    def request(self, *, path="/api/missions", host=None, origin="same", key="right", ctype="application/json",
                body=b'{"objective":"x"}', length=None, extra=""):
        host = host or f"127.0.0.1:{self.port}"
        headers = [f"POST {path} HTTP/1.1", f"Host: {host}"]
        if origin == "same":
            headers.append(f"Origin: http://127.0.0.1:{self.port}")
        elif origin:
            headers.append(f"Origin: {origin}")
        if key == "right":
            headers.append(f"X-Petron-Key: {self.secret}")
        elif key:
            headers.append(f"X-Petron-Key: {key}")
        if ctype:
            headers.append(f"Content-Type: {ctype}")
        headers.append(f"Content-Length: {len(body) if length is None else length}")
        return ("\r\n".join(headers) + "\r\n\r\n").encode() + body + extra.encode()

    def smuggled(self):
        """Request bytes hidden in a body: if a refusal leaves them unread they become a second request."""
        return (f"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n").encode()

    def assert_single_refusal(self, raw, label):
        reply = RawHttp(self.port).exchange(raw)
        statuses = reply.count(b"HTTP/1.1 ")
        self.assertEqual(statuses, 1, f"{label}: the leftover body was parsed as another request:\n{reply[:400]!r}")
        self.assertIn(b"Connection: close", reply, label)
        self.assertEqual(self.engine.store.missions(), [], label)       # nothing was created

    def test_refusals_close_the_connection_before_leftover_bytes_are_parsed(self):
        hidden = self.smuggled()
        cases = {
            "wrong_key": self.request(key="wrong" * 8, body=hidden),
            "no_key": self.request(key=None, body=hidden),
            "bad_origin": self.request(origin="http://evil.example", body=hidden),
            "no_origin": self.request(origin=None, body=hidden),
            "bad_host": self.request(host="evil.example", body=hidden),
            "bad_content_type": self.request(ctype="text/plain", body=hidden),
            "unknown_path": self.request(path="/api/other", body=hidden),
            "oversized_body": self.request(body=hidden, length=5000),
            "too_short_length": self.request(body=hidden, length=1),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assert_single_refusal(raw, label)

    def test_a_read_only_bridge_closes_after_refusing_a_post(self):
        readonly = BrainServer(self.home, "127.0.0.1", 0)
        threading.Thread(target=readonly.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (readonly.shutdown(), readonly.server_close()))
        raw = (f"POST /api/missions HTTP/1.1\r\nHost: 127.0.0.1:{readonly.port}\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(self.smuggled())}\r\n\r\n").encode() + self.smuggled()
        reply = RawHttp(readonly.port).exchange(raw)
        self.assertEqual(reply.count(b"HTTP/1.1 "), 1, reply[:300])
        self.assertTrue(reply.startswith(b"HTTP/1.1 405"))

    def test_chunked_posts_are_refused_and_not_reinterpreted(self):
        raw = (f"POST /api/missions HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: http://127.0.0.1:{self.port}\r\n"
               f"X-Petron-Key: {self.secret}\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
               ).encode() + b"0\r\n\r\n" + self.smuggled()
        reply = RawHttp(self.port).exchange(raw)
        self.assertEqual(reply.count(b"HTTP/1.1 "), 1, reply[:300])
        self.assertEqual(self.engine.store.missions(), [])

    def test_an_accepted_command_still_works_after_these_changes(self):
        reply = RawHttp(self.port).exchange(self.request(body=b'{"objective":"corrija"}'))
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"), reply[:200])
        self.assertEqual(len(self.engine.store.missions()), 1)

    def test_a_body_that_never_arrives_cannot_hold_a_handler_forever(self):
        self.assertTrue(0 < brain_bridge.BrainHandler.timeout <= 60)       # the shipped default is bounded
        with mock.patch.object(brain_bridge.BrainHandler, "timeout", 1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            sock.sendall(self.request(body=b"", length=50))
            started = time.monotonic()
            data = sock.recv(4096)          # the server gives up and closes (EOF or an error), it does not hang
            sock.close()
        self.assertLess(time.monotonic() - started, 8, data)

    def test_unexpected_failures_become_a_controlled_error_not_a_dropped_connection(self):
        with mock.patch.object(self.commands, "create_draft", side_effect=RuntimeError("boom")):
            reply = RawHttp(self.port).exchange(self.request(body=b'{"objective":"x"}'))
        self.assertTrue(reply.startswith(b"HTTP/1.1 500"), reply[:200])
        self.assertIn(b"internal_error", reply)
        self.assertNotIn(b"boom", reply)                                 # no internals on the wire


class LoopbackBindTests(unittest.TestCase):
    """Codex P2: the name `localhost` is never handed to the resolver when binding."""

    def test_localhost_binds_the_loopback_literal(self):
        captured = {}

        def fake_init(self, address, handler):
            captured["address"] = address
            raise OSError("stop before binding")

        with mock.patch.object(brain_bridge.ThreadingHTTPServer, "__init__", fake_init):
            with self.assertRaises(OSError):
                BrainServer(Path("/tmp/none"), "localhost", 0)
        self.assertEqual(captured["address"][0], "127.0.0.1")

    def test_non_loopback_hosts_stay_refused(self):
        for host in ("0.0.0.0", "192.0.2.1", "example.com", ""):
            with self.assertRaises(ValueError):
                BrainServer(Path("/tmp/none"), host, 0)


def failing_engine_factory(*, on_append=None, on_set_scope=None, on_cancel=None, sandbox=True):
    """An engine whose store fails on demand, to inject faults after `engine.create` persisted."""
    state = {"calls": 0}

    def factory(home):
        engine = Engine(home, sandbox_factory=lambda spec: E002FakeSandbox())
        real_append, real_scope, real_transition = (engine.store.append_event, engine.store.set_scope,
                                                    engine.store.transition)

        def append(mission_id, kind, payload):
            if on_append and kind == on_append[0] and state["calls"] < on_append[1]:
                state["calls"] += 1
                raise sqlite3.OperationalError("disk I/O error")
            return real_append(mission_id, kind, payload)

        def set_scope(*args, **kwargs):
            if on_set_scope and state["calls"] < on_set_scope:
                state["calls"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real_scope(*args, **kwargs)

        def transition(mission_id, current, target, **kwargs):
            if on_cancel and target is S.CANCELLED:
                raise sqlite3.OperationalError("database is locked")
            return real_transition(mission_id, current, target, **kwargs)

        engine.store.append_event, engine.store.set_scope, engine.store.transition = append, set_scope, transition
        return engine

    return factory


class DraftSetupFailureTests(CommandCase):
    """FIX 05: a failure after the mission was persisted never leaves an active orphan draft."""

    def commands_with(self, **faults):
        return BrainCommands(self.home, self.template, engine_factory=failing_engine_factory(**faults))

    def missions(self):
        return {row["mission_id"]: row for row in self.engine.store.missions()}

    def test_a_failed_brain_tag_cancels_the_draft_and_the_next_one_works(self):
        commands = self.commands_with(on_append=("brain.draft", 1))
        status, code = self.refused(commands.create_draft, "first")
        self.assertEqual((status, code), (500, "draft_failed"))
        rows = self.missions()
        self.assertEqual([r["state"] for r in rows.values()], ["cancelled"])
        self.assertEqual(list(rows.values())[0]["failure_reason"], "draft_setup_failed")
        self.assertTrue(commands.create_draft("second")["mission_id"])      # nothing is blocked

    def test_a_failure_while_planning_cancels_the_draft_too(self):
        commands = self.commands_with(on_set_scope=1)
        self.assertEqual(self.refused(commands.create_draft, "first"), (500, "draft_failed"))
        self.assertEqual([r["state"] for r in self.missions().values()], ["cancelled"])
        self.assertEqual(commands.create_draft("second")["state"], "awaiting_approval")

    def test_the_failure_and_the_cancellation_are_both_in_the_audit_trail(self):
        commands = self.commands_with(on_append=("brain.draft", 1))
        self.refused(commands.create_draft, "first")
        mission_id = next(iter(self.missions()))
        events = self.engine.store.events(mission_id)
        self.assertIn("brain.draft_failed", [e["kind"] for e in events])
        self.assertEqual(self.engine.store.verify_chain()[0], True)
        failure = next(e for e in events if e["kind"] == "brain.draft_failed")
        self.assertEqual(failure["payload"]["cancelled"], True)
        self.assertNotIn("disk I/O", json.dumps(failure["payload"]))        # class name only, no message echo

    def test_when_the_cancel_cannot_be_persisted_the_refusal_says_so(self):
        commands = self.commands_with(on_append=("brain.draft", 99), on_cancel=True)
        status, code = self.refused(commands.create_draft, "first")
        self.assertEqual((status, code), (503, "draft_failed_not_cancelled"))
        mission_id = next(iter(self.missions()))
        self.assertNotIn(self.engine.store.state(mission_id), (S.CANCELLED,))   # nothing pretends it was cancelled

    def test_validation_rejections_still_leave_nothing_behind(self):
        self.assertEqual(self.refused(self.commands.create_draft, "password=hunter2hunter2"), (400, "draft_rejected"))
        self.assertEqual(self.engine.store.missions(), [])

    def test_the_http_layer_maps_these_failures_to_controlled_json(self):
        commands = self.commands_with(on_append=("brain.draft", 1))
        secret = secrets.token_urlsafe(32)
        server = BrainServer(self.home, "127.0.0.1", 0, commands, secret)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("POST", "/api/missions", body=b'{"objective":"x"}', headers={
            "Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
            "Content-Type": "application/json", "X-Petron-Key": secret})
        response = conn.getresponse()
        self.assertEqual((response.status, json.loads(response.read())), (500, {"error": "draft_failed"}))
        conn.close()


class ProviderRefusalTests(CommandCase):
    """FIX 06: a run the runtime refuses after approval ends truthfully and is never silently reusable."""

    def commands_refusing(self, error):
        def agent_provider_factory(config, *, allow_real_model=False):
            raise error

        return BrainCommands(self.home, self.template, engine_factory=lambda home: Engine(
            home, sandbox_factory=lambda spec: E002FakeSandbox(), agent_provider_factory=agent_provider_factory))

    def approved_and_refused(self, error):
        commands = self.commands_refusing(error)
        draft = commands.create_draft("fix it")
        result = commands.approve_and_run(draft["mission_id"], draft["scope_sha256"])
        commands.wait()
        return commands, draft, result

    def test_a_refused_run_is_a_terminal_failure_with_an_auditable_event(self):
        commands, draft, result = self.approved_and_refused(ProviderError("unknown_policy"))
        self.assertNotEqual(result["state"], "running", "approval is not execution")
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.FAILED)
        row = self.engine.store.mission(draft["mission_id"])
        self.assertTrue(row["failure_reason"].startswith("run_refused:"), row["failure_reason"])
        events = self.engine.store.events(draft["mission_id"])
        kinds = [e["kind"] for e in events]
        refused = next(e for e in events if e["kind"] == "brain.run_refused")
        self.assertEqual(refused["payload"]["code"], "provider_error:unknown_policy")
        self.assertLess(kinds.index("plan.approved"), kinds.index("brain.run_refused"))
        self.assertNotIn("run.lease", kinds)
        self.assertNotIn("sandbox.probe", kinds)                              # the loop never started
        self.assertEqual(self.engine.store.verify_chain()[0], True)

    def test_the_approval_hash_is_preserved_and_cannot_be_reused(self):
        commands, draft, _ = self.approved_and_refused(ProviderError("unknown_policy"))
        approval = self.engine.store.approval_for(draft["mission_id"], draft["scope_sha256"])
        self.assertIsNotNone(approval)                                        # the record is kept, not erased
        self.assertEqual(self.refused(commands.approve_and_run, draft["mission_id"], draft["scope_sha256"]),
                         (409, "not_awaiting_approval"))
        self.assertEqual(self.refused(commands.discard, draft["mission_id"]), (409, "not_awaiting_approval"))

    def test_the_snapshot_does_not_restore_a_failed_run_as_a_pending_draft(self):
        commands, draft, _ = self.approved_and_refused(ProviderError("unknown_policy"))
        reader = EventReader(self.home / "petron.sqlite3")
        self.addCleanup(reader.close)
        current = snapshot(reader, bridge_id=commands.bridge_id)
        self.assertEqual(current["mission_state"], "failed")
        self.assertIsNone(current["draft"])
        self.assertNotIn("scope_sha256", current)

    def test_a_new_mission_is_possible_after_a_refused_run(self):
        commands, draft, _ = self.approved_and_refused(ProviderError("unknown_policy"))
        self.assertEqual(commands.create_draft("again")["state"], "awaiting_approval")

    def test_an_unexpected_error_before_the_loop_is_also_recorded_not_swallowed(self):
        commands, draft, _ = self.approved_and_refused(RuntimeError("boom"))
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.FAILED)
        refused = next(e for e in self.engine.store.events(draft["mission_id"]) if e["kind"] == "brain.run_refused")
        self.assertEqual(refused["payload"]["code"], "internal_error:RuntimeError")
        self.assertNotIn("boom", json.dumps(refused["payload"]))

    def test_an_operator_cancel_that_wins_the_race_is_not_overwritten(self):
        commands = self.commands_refusing(ProviderError("unknown_policy"))
        draft = commands.create_draft("fix it")
        self.engine.approve(draft["mission_id"], draft["scope_sha256"], "tester", "unit-test")
        self.engine.cancel(draft["mission_id"])
        commands._run(draft["mission_id"])
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.CANCELLED)

    def test_no_paid_call_is_possible_on_this_path(self):
        built = []

        def agent_provider_factory(config, *, allow_real_model=False):
            built.append(allow_real_model)
            raise ProviderError("real_model_not_allowed")

        commands = BrainCommands(self.home, self.template, engine_factory=lambda home: Engine(
            home, sandbox_factory=lambda spec: E002FakeSandbox(), agent_provider_factory=agent_provider_factory))
        draft = commands.create_draft("fix it")
        commands.approve_and_run(draft["mission_id"], draft["scope_sha256"])
        commands.wait()
        self.assertEqual(built, [False])                                       # the operator never opted in


class BridgeOwnershipTests(CommandCase):
    """Codex P1: a draft can be approved or discarded only through the bridge that created it."""

    def second_bridge(self):
        broader = dict(self.template)
        broader["permissions"] = {**self.template["permissions"]}
        return BrainCommands(self.home, broader, engine_factory=self.factory)

    def test_another_bridge_sharing_the_home_cannot_approve_or_discard_the_draft(self):
        draft = self.commands.create_draft("from bridge A")
        other = self.second_bridge()
        self.assertNotEqual(other.bridge_id, self.commands.bridge_id)
        self.assertEqual(self.refused(other.approve_and_run, draft["mission_id"], draft["scope_sha256"]),
                         (403, "not_a_brain_draft"))
        self.assertEqual(self.refused(other.discard, draft["mission_id"]), (403, "not_a_brain_draft"))
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.AWAITING_APPROVAL)
        kinds = [e["kind"] for e in self.engine.store.events(draft["mission_id"])]
        self.assertNotIn("plan.approved", kinds)
        self.commands.approve_and_run(draft["mission_id"], draft["scope_sha256"])     # the owner still can
        self.commands.wait()
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.COMPLETED)

    def test_a_draft_from_a_previous_run_without_an_owner_is_not_approvable(self):
        spec = e002_spec(self.source)
        spec["mission_id"] = "brain-20260101000000-abcdef"
        mission_id = self.engine.create(spec)
        self.engine.store.append_event(mission_id, "brain.draft", {"channel": "brain", "objective_sha256": "0" * 64})
        scope = self.engine.plan(mission_id)                     # the old payload shape has no bridge identity
        self.assertEqual(self.refused(self.commands.approve_and_run, mission_id, scope), (403, "not_a_brain_draft"))
        self.assertEqual(self.refused(self.commands.discard, mission_id), (403, "not_a_brain_draft"))

    def test_a_foreign_draft_neither_blocks_nor_appears_in_another_bridges_snapshot(self):
        draft = self.commands.create_draft("from bridge A")
        other = self.second_bridge()
        reader = EventReader(self.home / "petron.sqlite3")
        self.addCleanup(reader.close)
        theirs = snapshot(reader, bridge_id=other.bridge_id)
        self.assertIsNone(theirs["draft"])
        self.assertNotIn(draft["scope_sha256"], json.dumps(theirs))
        mine = snapshot(reader, bridge_id=self.commands.bridge_id)
        self.assertEqual((mine["draft"]["mission_id"], mine["draft"]["scope_sha256"]),
                         (draft["mission_id"], draft["scope_sha256"]))
        self.assertTrue(other.create_draft("from bridge B")["mission_id"])      # not blocked by A's draft

    def test_the_read_only_bridge_exposes_no_draft_authority(self):
        draft = self.commands.create_draft("pending")
        reader = EventReader(self.home / "petron.sqlite3")
        self.addCleanup(reader.close)
        current = snapshot(reader)                                              # no bridge identity
        self.assertIsNone(current["draft"])
        self.assertNotIn(draft["scope_sha256"], json.dumps(current))


if __name__ == "__main__":
    unittest.main()
