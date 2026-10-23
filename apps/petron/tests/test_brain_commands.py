"""Mission entry from the Brain: drafts, hold-to-approve, discard, and the HTTP guards."""

import http.client
import json
import secrets
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from petron.brain_bridge import BRAIN_DIR, BrainServer, EventReader, snapshot
from petron.brain_commands import BrainCommands, CommandRefused, load_template
from petron.engine import Engine
from petron.mission import MissionState

from support import E002, E002FakeSandbox, e002_spec

S = MissionState


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-brain-cmd-"))
        self.home = self.tmp / "home"
        self.source = self.tmp / "source"
        shutil.copytree(E002 / "project", self.source)
        self.template = e002_spec(self.source)
        self.factory = lambda home: Engine(home, sandbox_factory=lambda spec: E002FakeSandbox())
        self.commands = BrainCommands(self.home, self.template, engine_factory=self.factory)
        self.engine = Engine(self.home)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self.engine.close)

    def refused(self, fn, *args):
        with self.assertRaises(CommandRefused) as caught:
            fn(*args)
        return caught.exception.status, caught.exception.code


class TemplateTests(CommandCase):
    def test_plan_mode_and_unauthorized_real_model_templates_are_refused(self):
        plan = dict(self.template)
        plan.pop("mode")
        with self.assertRaises(ValueError):
            BrainCommands(self.home, plan)
        paid = {**self.template, "provider": {"kind": "anthropic"},
                "budget": {**self.template["budget"], "max_model_calls": 4, "max_total_tokens": 24000}}
        with self.assertRaises(ValueError):
            BrainCommands(self.home, paid)
        BrainCommands(self.home, paid, allow_real_model=True)   # explicit operator opt-in
    def test_malformed_operator_templates_fail_closed(self):
        for value in ([], 17, {"workspace": []}, {"workspace": {"source": 22}}):
            path = self.tmp / "invalid-template.json"
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_template(path)


class DraftTests(CommandCase):
    def test_draft_waits_for_approval_and_records_its_origin(self):
        draft = self.commands.create_draft("  Corrija os testes do merge\x07  ")
        mission_id = draft["mission_id"]
        self.assertEqual(self.engine.store.state(mission_id), S.AWAITING_APPROVAL)
        spec = self.engine.store.spec(mission_id)
        self.assertEqual(spec.raw["objective"], "Corrija os testes do merge")   # control chars stripped
        self.assertEqual(spec.tools, frozenset(self.template["permissions"]["tools"]))  # scope from the template
        self.assertEqual(draft["scope_sha256"], self.engine.store.mission(mission_id)["plan_sha256"])
        kinds = [e["kind"] for e in self.engine.store.events(mission_id)]
        self.assertIn("brain.draft", kinds)
        self.assertNotIn("plan.approved", kinds)
        self.assertNotIn("run.lease", kinds)

    def test_objective_validation_and_single_active_draft(self):
        for bad in ("", "   ", "x" * 501, None, 42, ["a"]):
            self.assertEqual(self.refused(self.commands.create_draft, bad)[0], 400, bad)
        self.commands.create_draft("first")
        self.assertEqual(self.refused(self.commands.create_draft, "second"), (409, "draft_active"))


class ApprovalTests(CommandCase):
    def test_hold_approval_runs_the_exact_draft(self):
        draft = self.commands.create_draft("fix it")
        self.assertEqual(self.refused(self.commands.approve_and_run, draft["mission_id"], "0" * 64),
                         (409, "plan_hash_mismatch"))
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.AWAITING_APPROVAL)
        self.commands.approve_and_run(draft["mission_id"], draft["scope_sha256"])
        self.commands.wait()
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.COMPLETED)
        events = self.engine.store.events(draft["mission_id"])
        approved = next(i for i, e in enumerate(events) if e["kind"] == "plan.approved")
        running = next(i for i, e in enumerate(events) if e["kind"] == "mission.state"
                       and e["payload"]["to"] == "running")
        self.assertLess(approved, running)
        self.assertEqual(events[approved]["payload"]["channel"], "brain-hold-gesture")
        self.assertEqual(self.refused(self.commands.approve_and_run, draft["mission_id"], draft["scope_sha256"]),
                         (409, "not_awaiting_approval"))

    def test_cli_missions_cannot_be_approved_or_discarded_from_the_brain(self):
        spec = e002_spec(self.source)
        spec["mission_id"] = "brain-20260101000000-abcdef"   # even with a Brain-looking id
        mission_id = self.engine.create(spec)
        scope = self.engine.plan(mission_id)
        self.assertEqual(self.refused(self.commands.approve_and_run, mission_id, scope), (403, "not_a_brain_draft"))
        self.assertEqual(self.refused(self.commands.discard, mission_id), (403, "not_a_brain_draft"))
        self.assertEqual(self.engine.store.state(mission_id), S.AWAITING_APPROVAL)

    def test_discard_cancels_without_running(self):
        draft = self.commands.create_draft("never mind")
        self.commands.discard(draft["mission_id"])
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.CANCELLED)
        kinds = [e["kind"] for e in self.engine.store.events(draft["mission_id"])]
        self.assertNotIn("plan.approved", kinds)
        self.commands.create_draft("a new one is now allowed")

    def test_snapshot_exposes_the_pending_brain_draft_for_reloads(self):
        draft = self.commands.create_draft("pending")
        reader = EventReader(self.home / "petron.sqlite3")
        self.addCleanup(reader.close)
        current = snapshot(reader, bridge_id=self.commands.bridge_id)
        self.assertEqual((current["state"], current["draft"]["mission_id"], current["draft"]["scope_sha256"]),
                         ("awaiting_approval", draft["mission_id"], draft["scope_sha256"]))


class EndpointTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.secret = secrets.token_urlsafe(32)
        self.server = BrainServer(self.home, "127.0.0.1", 0, self.commands, self.secret)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.origin = f"http://127.0.0.1:{self.server.port}"

    def post(self, path, body, **overrides):
        headers = {"Host": f"127.0.0.1:{self.server.port}", "Origin": self.origin,
                   "Content-Type": "application/json", "X-Petron-Key": self.secret}
        headers.update({k.replace("_", "-"): v for k, v in overrides.items()})
        headers = {k: v for k, v in headers.items() if v is not None}
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("POST", path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        try:
            return response.status, json.loads(payload)
        except ValueError:
            return response.status, payload

    def test_guards(self):
        body = {"objective": "x"}
        self.assertEqual(self.post("/api/missions", body, X_Petron_Key=None)[0], 403)
        self.assertEqual(self.post("/api/missions", body, X_Petron_Key="wrong" * 10)[0], 403)
        self.assertEqual(self.post("/api/missions", body, Origin=None)[0], 403)
        self.assertEqual(self.post("/api/missions", body, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/missions", body, Sec_Fetch_Site="cross-site")[0], 403)
        self.assertEqual(self.post("/api/missions", body, Content_Type="text/plain")[0], 415)
        self.assertEqual(self.post("/api/missions", {"objective": "x", "tools": ["shell"]})[0], 400)
        self.assertEqual(self.post("/api/missions", {"objective": "x" * 3000})[0], 413)
        self.assertEqual(self.post("/api/missions", b"{not json")[0], 400)
        self.assertEqual(self.post("/api/missions/../../etc", body)[0], 404)
        self.assertEqual(self.engine.store.missions(), [])          # nothing was created by any of these

    def test_secret_never_appears_in_served_content(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        for path in ("/", "/live.js", "/brain.js", "/api/snapshot"):
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.server.port}"})
            body = conn.getresponse().read().decode("utf-8", "replace")
            self.assertNotIn(self.secret, body, path)
        conn.close()

    def test_full_flow_over_http(self):
        status, draft = self.post("/api/missions", {"objective": "corrija o defeito"})
        self.assertEqual(status, 200)
        status, _ = self.post(f"/api/missions/{draft['mission_id']}/approve", {"scope_sha256": "1" * 64})
        self.assertEqual(status, 409)
        status, result = self.post(f"/api/missions/{draft['mission_id']}/approve",
                                   {"scope_sha256": draft["scope_sha256"]})
        self.assertEqual((status, result["state"]), (200, "approved"))
        self.commands.wait()
        self.assertEqual(self.engine.store.state(draft["mission_id"]), S.COMPLETED)

    def test_weak_secret_refused(self):
        with self.assertRaises(ValueError):
            BrainServer(self.home, "127.0.0.1", 0, self.commands, "short")

    def test_hidden_input_exists_only_on_an_interactive_bridge(self):
        standalone = (BRAIN_DIR / "index.html").read_text()
        self.assertNotIn("petron-input", standalone)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.server.port}"})
        live = conn.getresponse().read().decode()
        conn.close()
        self.assertIn('id="petron-input" class="petron-status"', live)   # visually hidden, screen-reader labelled
        readonly = BrainServer(self.home, "127.0.0.1", 0)
        threading.Thread(target=readonly.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (readonly.shutdown(), readonly.server_close()))
        conn = http.client.HTTPConnection("127.0.0.1", readonly.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{readonly.port}"})
        self.assertNotIn("petron-input", conn.getresponse().read().decode())   # read-only bridge: no input at all
        conn.close()


if __name__ == "__main__":
    unittest.main()
