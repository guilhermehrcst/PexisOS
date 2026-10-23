"""Brain bridge: event mapping, snapshots, reconnection and the local server's refusals."""

import datetime as dt
import http.client
import json
import re
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from petron.brain_bridge import (BRAIN_DIR, FRESH_S, BrainServer, EventReader, brain_state_for, runtime_message,
                                 snapshot)
from petron.mission import MissionState
from petron.store import Store

from support import E002, e002_spec

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


def at(seconds_ago: float) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event(kind, payload=None, age=1.0):
    return {"seq": 1, "mission_id": "m", "kind": kind, "payload": payload or {}, "at": at(age)}


class MappingTests(unittest.TestCase):
    def state(self, kind, payload=None, age=1.0):
        mapped = brain_state_for(event(kind, payload, age), NOW)
        return mapped and mapped["state"]

    def test_mission_transitions(self):
        self.assertEqual(self.state("mission.state", {"to": "planning"}), "thinking")
        self.assertEqual(self.state("mission.state", {"to": "awaiting_approval"}), "awaiting_approval")
        self.assertEqual(self.state("mission.state", {"to": "running"}), "working")
        self.assertEqual(self.state("mission.state", {"to": "verifying"}), "verifying")
        self.assertEqual(self.state("mission.state", {"to": "cancelled"}), "idle")

    def test_success_and_error_only_when_fresh(self):
        self.assertEqual(self.state("mission.state", {"to": "completed"}), "success")
        self.assertEqual(self.state("mission.state", {"to": "failed"}), "error")
        self.assertEqual(self.state("mission.state", {"to": "completed"}, age=FRESH_S + 1), "idle")
        self.assertEqual(self.state("mission.state", {"to": "failed"}, age=FRESH_S + 1), "idle")
        self.assertEqual(self.state("mission.state", {"to": "completed"}, age=-5), "idle")  # clock skew: not fresh

    def test_agent_and_tool_events(self):
        self.assertEqual(self.state("agent.decision_requested"), "thinking")
        self.assertEqual(self.state("tool.authorization", {"decision": "allow", "effect": "execute"}), "working")
        self.assertEqual(self.state("tool.authorization", {"decision": "deny"}), "error")
        self.assertEqual(self.state("agent.verification_started"), "verifying")
        self.assertEqual(self.state("plan.approved"), "idle")   # approval recorded is not execution

    def test_activity_is_a_fixed_heuristic(self):
        execute = brain_state_for(event("tool.authorization", {"decision": "allow", "effect": "execute"}), NOW)
        read = brain_state_for(event("tool.authorization", {"decision": "allow", "effect": "read"}), NOW)
        self.assertGreater(execute["activity"], read["activity"])

    def test_unknown_or_malformed_events_change_nothing(self):
        self.assertIsNone(brain_state_for(event("agent.checkpoint"), NOW))
        self.assertIsNone(brain_state_for({"kind": "mission.state", "payload": "garbage", "at": "x"}, NOW))
        self.assertIsNone(brain_state_for({}, NOW))
        self.assertIsNone(self.state("mission.state", {"to": "teleported"}))

    def test_wire_message_never_carries_payload_content(self):
        message = runtime_message({**event("tool.result", {"result": "SECRET FILE TEXT"}), "seq": 9}, NOW)
        self.assertNotIn("SECRET", json.dumps(message))
        self.assertEqual((message["seq"], message["kind"], message["state"]), (9, "tool.result", "working"))


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-brain-"))
        self.home = self.tmp / "home"
        self.store = Store(self.home / "petron.sqlite3")
        source = self.tmp / "source"
        shutil.copytree(E002 / "project", source)
        from petron.mission import MissionSpec
        self.spec = MissionSpec.parse(e002_spec(source))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reader(self):
        reader = EventReader(self.home / "petron.sqlite3")
        self.addCleanup(reader.close)
        return reader


class SnapshotTests(BridgeCase):
    def test_empty_store_is_idle(self):
        self.assertEqual(snapshot(EventReader(self.tmp / "missing.sqlite3"))["state"], "idle")

    def test_awaiting_approval_and_running(self):
        self.store.create_mission(self.spec)
        self.store.transition("petron-e002", MissionState.CREATED, MissionState.PLANNING)
        self.store.transition("petron-e002", MissionState.PLANNING, MissionState.AWAITING_APPROVAL)
        self.assertEqual(snapshot(self.reader())["state"], "awaiting_approval")

    def test_running_mission_with_a_dead_runner_is_interrupted_not_active(self):
        self.store.create_mission(self.spec)
        for a, b in (("created", "planning"), ("planning", "awaiting_approval"), ("awaiting_approval", "running")):
            self.store.transition("petron-e002", MissionState(a), MissionState(b))
        self.store.append_event("petron-e002", "run.lease", {"pid": 2 ** 22 + 12345, "host": socket.gethostname(),
                                                            "token": "dead"})
        self.store.append_event("petron-e002", "agent.decision_requested", {"iteration": 1})
        current = snapshot(self.reader())
        self.assertTrue(current["interrupted"])
        self.assertEqual(current["state"], "idle")

    def test_old_completion_is_not_replayed(self):
        self.store.create_mission(self.spec)
        old = Store(self.home / "petron.sqlite3", clock=lambda: at(3600))
        for a, b in (("created", "planning"), ("planning", "awaiting_approval"), ("awaiting_approval", "running"),
                     ("running", "verifying"), ("verifying", "completed")):
            old.transition("petron-e002", MissionState(a), MissionState(b))
        old.close()
        self.assertEqual(snapshot(self.reader())["state"], "idle")


class ServerCase(BridgeCase):
    def setUp(self):
        super().setUp()
        self.server = BrainServer(self.home, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request(method, path, headers={"Host": f"127.0.0.1:{self.server.port}", **(headers or {})})
        response = conn.getresponse()
        body = response.read() if method != "HEAD" else b""
        conn.close()
        return response, body

    def stream(self, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.server.port}", **(headers or {})})
        response = conn.getresponse()
        self.addCleanup(conn.close)
        return response

    @staticmethod
    def read_messages(response, count):
        messages, block = [], {}
        while len(messages) < count:
            line = response.fp.readline().decode().rstrip("\n")
            if line.startswith("event:"):
                block["event"] = line[6:].strip()
            elif line.startswith("data:"):
                block["data"] = json.loads(line[5:].strip())
            elif line.startswith("id:"):
                block["id"] = int(line[3:].strip())
            elif line == "" and "data" in block:
                messages.append(block)
                block = {}
        return messages


class ServerSecurityTests(ServerCase):
    def test_assets_are_an_allowlist(self):
        for path in ("/", "/brain.js", "/live.js", "/brain-texture.webp"):
            self.assertEqual(self.request(path)[0].status, 200, path)
        for path in ("/README.md", "/../petron.sqlite3", "/brain/../../src/petron/store.py", "/api/missions"):
            self.assertEqual(self.request(path)[0].status, 404, path)

    def test_cross_origin_and_rebinding_are_refused(self):
        self.assertEqual(self.request("/api/snapshot", headers={"Host": "evil.example"})[0].status, 403)
        self.assertEqual(self.request("/api/snapshot", headers={"Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.request("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        same = f"http://127.0.0.1:{self.server.port}"
        self.assertEqual(self.request("/api/snapshot", headers={"Origin": same})[0].status, 200)

    def test_read_only_and_headers(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request("/api/snapshot", method=method)[0].status, 405, method)
        response, body = self.request("/")
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        html = body.decode()
        self.assertIn('<script src="./live.js" defer></script>', html)
        self.assertIsNone(re.search(r"https?://", html.replace("http://www.w3.org", "")))

    def test_only_loopback_binds(self):
        with self.assertRaises(ValueError):
            BrainServer(self.home, "0.0.0.0", 0)


class ServerStreamTests(ServerCase):
    def test_snapshot_then_ordered_runtime_events(self):
        self.store.create_mission(self.spec)
        response = self.stream()
        first = self.read_messages(response, 1)[0]
        self.assertEqual(first["event"], "snapshot")
        self.store.transition("petron-e002", MissionState.CREATED, MissionState.PLANNING)
        self.store.append_event("petron-e002", "agent.decision_requested", {"iteration": 1})
        messages = self.read_messages(response, 2)
        self.assertEqual([m["data"]["state"] for m in messages], ["thinking", "thinking"])
        self.assertLess(messages[0]["id"], messages[1]["id"])
        self.assertGreater(messages[0]["id"], first["id"])

    def test_reconnect_catches_up_then_sends_authoritative_snapshot(self):
        self.store.create_mission(self.spec)
        resume_from = EventReader(self.home / "petron.sqlite3").max_seq()
        self.store.transition("petron-e002", MissionState.CREATED, MissionState.PLANNING)
        self.store.transition("petron-e002", MissionState.PLANNING, MissionState.AWAITING_APPROVAL)
        response = self.stream({"Last-Event-ID": str(resume_from)})
        messages = self.read_messages(response, 3)
        self.assertEqual([m["event"] for m in messages], ["runtime", "runtime", "snapshot"])
        self.assertTrue(all(m["id"] > resume_from for m in messages[:2]))
        self.assertEqual(messages[-1]["data"]["state"], "awaiting_approval")


class AssetTests(unittest.TestCase):
    def test_standalone_page_stays_visual_only(self):
        html = (BRAIN_DIR / "index.html").read_text()
        self.assertNotIn("live.js", html)
        self.assertNotRegex(html, r"<(button|nav|a|input|textarea|select|form)\b")

    def test_scripts_make_no_mutating_or_remote_calls(self):
        brain = (BRAIN_DIR / "brain.js").read_text()
        live = (BRAIN_DIR / "live.js").read_text()
        for source in (brain, live):
            self.assertNotRegex(source, r"XMLHttpRequest|WebSocket|sendBeacon|https?://")
            self.assertNotRegex(source, r"getUserMedia|SpeechRecognition")   # no audio capture in this increment
        self.assertNotRegex(brain, r"\bfetch\(")
        self.assertIn("new EventSource('/api/events')", live)
        # The only writes are same-origin POSTs to the three mission commands, carrying the in-memory key.
        self.assertEqual(live.count("window.fetch("), 1)
        self.assertIn("method: 'POST', credentials: 'same-origin'", live)
        self.assertEqual(set(re.findall(r"command\(`?'?(/api/[^`',]+)", live)),
                         {"/api/missions", "/api/missions/${target.mission_id}/approve",
                          "/api/missions/${target.mission_id}/discard"})

    def test_v01_api_is_preserved(self):
        brain = (BRAIN_DIR / "brain.js").read_text()
        for name in ("setState", "setActivity", "getState", "pause", "resume"):
            self.assertIn(name, brain)
        for mode in ("idle", "thinking", "working", "success", "error", "listening", "verifying",
                     "awaiting_approval", "disconnected"):
            self.assertRegex(brain, rf"\b{mode}: \{{")


if __name__ == "__main__":
    unittest.main()
