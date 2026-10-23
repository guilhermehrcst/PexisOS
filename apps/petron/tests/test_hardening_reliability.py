"""Reliability and honesty fixes found in review: malformed input never produces a traceback,
planning cannot strand a mission, retries are never hidden, reports cannot repaint a terminal,
the event log says what it can and cannot prove, and the installed wheel works."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from petron.cli import main as cli_main
from petron.engine import EngineError
from petron.mission import MissionState
from petron.providers.anthropic_provider import AnthropicProvider
from petron.report import build_report, render_markdown, terminal_safe
from petron.store import Store

from hardening_support import HardeningCase, real_provider_spec
from support import E001, EngineCase, e001_spec


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MalformedInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-malformed-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def create(self, text):
        path = self.tmp / "spec.json"
        path.write_text(text)
        return cli("--home", str(self.tmp / "home"), "mission", "create", str(path))

    def test_wrong_json_types_exit_2_without_a_traceback(self):
        for text in ("[]", "42", '"x"', "null", "{not json", "",
                     '{"workspace": 5}', '{"workspace": [1]}', '{"workspace": {"source": 5}}',
                     '{"workspace": {"source": ["a"]}}', '{"workspace": null}'):
            code, out, err = self.create(text)
            self.assertEqual(code, 2, (text, out, err))
            self.assertNotIn("Traceback", out + err, text)
            self.assertTrue(err.startswith("error:"), (text, err))

    def test_experiment_with_a_missing_fixture_is_a_clean_error(self):
        import petron.cli as module
        original = module.FIXTURES
        module.FIXTURES = self.tmp / "no-fixtures"
        self.addCleanup(lambda: setattr(module, "FIXTURES", original))
        code, out, err = cli("--home", str(self.tmp / "home"), "experiment", "e001", "--sandbox", "none")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)

    def test_source_with_a_symlink_is_a_clean_error_on_create(self):
        source = self.tmp / "source"
        shutil.copytree(E001 / "project", source)
        (source / "leak.py").symlink_to("/etc/passwd")
        spec = e001_spec(source)
        code, out, err = self.create(json.dumps(spec))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)


class PlanningCannotStrandAMission(EngineCase):
    def test_workspace_error_during_planning_fails_the_mission(self):
        mission_id = self.engine.create(self.spec())
        (self.source / "calc" / "leak.py").symlink_to("/etc/passwd")   # appears after create
        with self.assertRaises(EngineError) as caught:
            self.engine.plan(mission_id)
        self.assertTrue(caught.exception.code.startswith("internal_error:"))
        self.assertEqual(self.engine.store.state(mission_id), MissionState.FAILED)
        self.assertTrue(self.engine.store.mission(mission_id)["failure_reason"])

    def test_unexpected_provider_exception_fails_the_mission(self):
        class Exploding:
            kind, simulated = "deterministic", True

            def propose_plan(self, request):
                raise ZeroDivisionError("boom")
        self.engine.provider_factory = lambda config, allow_real_model=False: Exploding()
        mission_id = self.engine.create(self.spec())
        with self.assertRaises(EngineError):
            self.engine.plan(mission_id)
        self.assertEqual(self.engine.store.state(mission_id), MissionState.FAILED)


class RetryAccountingTests(unittest.TestCase):
    def test_sdk_automatic_retries_are_disabled(self):
        seen = {}

        class Anthropic:
            def __init__(self, **kwargs):
                seen.update(kwargs)
        fake = types.ModuleType("anthropic")
        fake.Anthropic = Anthropic
        import os
        previous = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "fake-key-for-test-only"
        sys.modules["anthropic"] = fake
        try:
            AnthropicProvider("claude-opus-5-5", "high", 1000)._build_client()
        finally:
            sys.modules.pop("anthropic", None)
            if previous is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = previous
        self.assertEqual(seen["max_retries"], 0, "a hidden retry would be an unaccounted, billable request")
        self.assertEqual(seen["base_url"], "https://api.anthropic.com")

    def test_one_provider_call_is_one_request(self):
        calls = []

        class Client:
            beta = types.SimpleNamespace(messages=types.SimpleNamespace(
                create=lambda **kw: (calls.append(kw), (_ for _ in ()).throw(TimeoutError("slow")))[1]))
        from petron.providers.base import PlanRequest, ProviderError
        request = PlanRequest("m", "Fix it", {"run_tests": {"effect": "execute", "description": "d",
                                                            "input_schema": {}}}, {}, (), (), 2)
        with self.assertRaises(ProviderError):
            AnthropicProvider("claude-opus-5-5", "high", 1000, client=Client()).propose_plan(request)
        self.assertEqual(len(calls), 1)


class TerminalSafetyTests(HardeningCase):
    ESC = "\x1b"

    def test_terminal_safe_escapes_controls_but_keeps_layout(self):
        text = f"a\tb\nc{self.ESC}[2J\x07\x00\x9b‮⁦d"
        safe = terminal_safe(text)
        for char in (self.ESC, "\x07", "\x00", "\x9b", "‮", "⁦"):
            self.assertNotIn(char, safe)
        self.assertIn("\\x1b", safe)
        self.assertIn("a\tb\nc", safe)

    def test_a_hostile_diff_cannot_inject_escape_sequences_or_break_the_fence(self):
        mission_id = self.engine.create(real_provider_spec(self.source))
        store = self.engine.store
        store.put_artifact(mission_id, "diff.patch",
                           f"--- a/x\n+++ b/x\n+{self.ESC}]52;c;ZXZpbA==\x07 clipboard\n+```\n# injected heading\n")
        report = build_report(store, mission_id)
        markdown = render_markdown(report)
        self.assertNotIn(self.ESC, markdown)
        self.assertNotIn("\x07", markdown)
        fence_lines = [line for line in markdown.splitlines() if line.startswith("`")]
        opener = next(line for line in fence_lines if line.startswith("`") and "diff" in line)
        self.assertGreaterEqual(len(opener) - len("diff"), 4, "the fence must be longer than any backtick run inside")
        self.assertIn(self.ESC, report["diff"], "the raw artifact is preserved; only rendering is escaped")

    def test_hostile_text_in_other_report_fields_is_escaped(self):
        mission_id = self.engine.create(e001_spec(self.source))
        report = build_report(self.engine.store, mission_id)
        report["objective"] = f"x{self.ESC}[31mred"
        report["failure_reason"] = f"y{self.ESC}[0m"
        markdown = render_markdown(report)
        self.assertNotIn(self.ESC, markdown)


class EventLogHonestyTests(unittest.TestCase):
    """The hash chain is unkeyed: it detects accidental damage and naive edits. Whoever can rewrite
    the database can recompute it. Truncation of the newest events leaves a valid prefix, so a head
    anchor is kept beside the database and can be recorded somewhere independent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="petron-anchor-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store = Store(self.tmp / "petron.sqlite3")
        self.addCleanup(self.store.close)
        for n in range(4):
            self.store.append_event(None, "note", {"n": n})

    def truncate_newest(self):
        raw = self.store.raw()
        raw.execute("DROP TRIGGER events_no_delete")
        raw.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")

    def test_truncating_the_newest_event_is_detected_by_the_local_anchor(self):
        self.assertEqual(self.store.verify_chain(), (True, None))
        self.truncate_newest()
        ok, bad = self.store.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 4)

    def test_an_independently_recorded_head_detects_truncation_even_without_the_anchor_file(self):
        head = self.store.head()
        self.assertEqual(head["seq"], 4)
        self.truncate_newest()
        (self.tmp / "petron.sqlite3.anchor").unlink()
        self.assertEqual(self.store.verify_chain(), (True, None), "the chain alone cannot see this")
        self.assertFalse(self.store.verify_chain(expected_head=head)[0])
        self.assertTrue(self.store.verify_chain(expected_head={"seq": 3, "hash": self.store.head()["hash"]})[0])

    def test_rolling_back_to_an_earlier_valid_state_is_detected_with_a_recorded_head(self):
        recorded = self.store.head()
        self.store.append_event(None, "note", {"n": 99})
        self.assertTrue(self.store.verify_chain(expected_head=recorded)[0])   # a prefix of the recorded head is fine
        raw = self.store.raw()
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json = '{\"n\":7}' WHERE seq = 2")
        self.assertFalse(self.store.verify_chain(expected_head=recorded)[0])

    def test_cli_reports_the_limit_and_accepts_an_expected_head(self):
        home = self.tmp   # the same directory (and database) the store above writes to
        code, out, _ = cli("--home", str(home), "events", "verify")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIn("unkeyed", report["note"])
        code, out, _ = cli("--home", str(home), "events", "head")
        self.assertEqual(code, 0)
        head = json.loads(out)
        self.assertIn("seq", head)
        code, out, _ = cli("--home", str(home), "events", "verify", "--expect-head", f"{head['seq']}:{head['hash']}")
        self.assertEqual(code, 0)
        code, _, err = cli("--home", str(home), "events", "verify", "--expect-head", "5:" + "0" * 64)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
