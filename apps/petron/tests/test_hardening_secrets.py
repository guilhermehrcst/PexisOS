"""No recognizable credential is persisted, shown or sent: not in mission specs, not in reports,
not in provider requests. Synthetic credentials only; nothing here reaches a network."""

import json
import types
import unittest

from petron.cli import main as cli_main
from petron.engine import EngineError
from petron.mission import MissionSpec, canonical_json
from petron.providers.anthropic_provider import AnthropicProvider
from petron.providers.base import PlanRequest, ProviderError
from petron.redact import REDACTED, contains_secret, redact_text
from petron.report import build_report, render_markdown
from petron.store import StoreError

from hardening_support import FAKE_ANTHROPIC, FAKE_ASSIGNED, FAKE_GITHUB, HardeningCase, real_provider_spec
from support import e001_spec


class RedactionShapeTests(unittest.TestCase):
    def test_quoted_values_with_spaces_are_fully_redacted(self):
        cases = [
            "password='correct horse battery staple'",
            'api_key = "correct horse battery staple"',
            "secret: 'two words'",
            'token="a \\"quoted\\" phrase with spaces"',
            "client_secret='it\\'s a long one'",
            'password="line one\nline two with the rest"',
            'config = {"password": "correct horse battery staple"}',
            'JSON-in-a-string: password=\\"correct horse battery staple\\" done',
            'password="never closed because the log was cut',
        ]
        for text in cases:
            out = redact_text(text)
            for fragment in ("correct", "horse", "battery", "staple", "two words", "line two", "quoted", "long one",
                             "cut"):
                self.assertNotIn(fragment, out, (text, out))
            self.assertIn(REDACTED, out, text)

    def test_ordinary_text_is_not_masked(self):
        ordinary = [
            "The token budget is 5 per call.",
            "max_tokens limit is 4096 and password policy requires length",
            "token: 'a' is a short value",
            "Use the secret santa list: ask the team",
            "input_tokens=1234 output_tokens=567",
            'He said "the password is in the vault" and left',
        ]
        for text in ordinary:
            self.assertEqual(redact_text(text), text)

    def test_redaction_is_idempotent_and_bounded(self):
        once = redact_text("password='a b c d' tail")
        self.assertEqual(redact_text(once), once)
        hostile = 'password="' + "x" * 200000   # no closing quote: must stay fast and bounded
        self.assertLess(len(redact_text(hostile)), 300000)

    def test_contains_secret(self):
        self.assertTrue(contains_secret(f"token={FAKE_ASSIGNED}"))
        self.assertTrue(contains_secret({"a": ["x", FAKE_ANTHROPIC]}))
        self.assertFalse(contains_secret("Fix the median function in calc/stats.py"))


class SpecPersistenceTests(HardeningCase):
    def secret_spec(self, objective):
        spec = e001_spec(self.source)
        spec["objective"] = objective
        return spec

    def test_secret_bearing_objective_is_rejected_before_anything_is_stored(self):
        for objective in (f"Fix the bug. Log said token={FAKE_ASSIGNED}", f"use {FAKE_ANTHROPIC} to call",
                          f"password='{FAKE_ASSIGNED} with spaces'", f"clone with {FAKE_GITHUB}"):
            with self.assertRaises(EngineError) as caught:
                self.engine.create(self.secret_spec(objective))
            self.assertEqual(caught.exception.code, "secret_in_spec")
            self.assertNotIn(FAKE_ASSIGNED.encode(), str(caught.exception).encode())
        raw = self.engine.store.raw()
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM missions").fetchone()[0], 0)
        blob = self.db_bytes()
        for secret in (FAKE_ASSIGNED, FAKE_ANTHROPIC, FAKE_GITHUB):
            self.assertNotIn(secret.encode(), blob)

    def test_store_refuses_a_secret_even_when_the_engine_is_bypassed(self):
        spec = MissionSpec.parse(self.secret_spec(f"token={FAKE_ASSIGNED}"))
        with self.assertRaises(StoreError) as caught:
            self.engine.store.create_mission(spec)
        self.assertEqual(caught.exception.code, "secret_in_spec")
        self.assertNotIn(FAKE_ASSIGNED.encode(), self.db_bytes())

    def test_clean_mission_is_unaffected_and_hash_is_stable(self):
        spec = e001_spec(self.source)
        mission_id = self.engine.create(spec)
        stored = self.engine.store.spec(mission_id)
        self.assertEqual(stored.sha256, MissionSpec.parse(spec).sha256)
        self.assertEqual(canonical_json(stored.raw), canonical_json(MissionSpec.parse(spec).raw))

    def test_cli_refuses_with_a_clean_error_and_never_echoes_the_secret(self):
        spec_path = self.tmp / "spec.json"
        spec_path.write_text(json.dumps(self.secret_spec(f"token={FAKE_ASSIGNED}")))
        out, err = _capture(lambda: cli_main(["--home", str(self.tmp / "home2"), "mission", "create", str(spec_path)]))
        self.assertEqual(out[0], 2)
        self.assertIn("secret_in_spec", out[2])
        self.assertNotIn(FAKE_ASSIGNED, out[1] + out[2])

    def test_a_legacy_row_holding_a_secret_is_still_not_disclosed_by_read_paths(self):
        """Rows written by an older version may already contain plaintext. The disclosure paths
        (report, show) must not repeat it."""
        mission_id = self.engine.create(e001_spec(self.source))
        legacy = json.loads(self.engine.store.mission(mission_id)["spec_json"])
        legacy["objective"] = f"legacy objective token={FAKE_ASSIGNED}"
        legacy["mission_id"] = "legacy-mission"
        raw = self.engine.store.raw()
        raw.execute("INSERT INTO missions (mission_id, spec_json, spec_sha256, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'created', 't', 't')",
                    ("legacy-mission", canonical_json(legacy), MissionSpec.parse(legacy).sha256))
        report = build_report(self.engine.store, "legacy-mission")
        shown = json.dumps(report) + render_markdown(report)
        self.assertNotIn(FAKE_ASSIGNED, shown)
        self.assertIn(REDACTED, shown)
        out, _ = _capture(lambda: cli_main(["--home", str(self.tmp / "home"), "mission", "report", "legacy-mission"]))
        self.assertNotIn(FAKE_ASSIGNED, out[1])


class ProviderRequestTests(HardeningCase):
    """What the Anthropic provider would send, captured by a fake client."""

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("stop here: the request was captured")

    def request(self, objective="Fix the median bug", files=None):
        return PlanRequest("m-1", objective, {"run_tests": {"effect": "execute", "description": "d",
                                                            "input_schema": {}}},
                           files or {"calc/stats.py": "x = 1"}, ("calc/stats.py",), ("tests",), 4)

    def test_objective_with_a_secret_never_reaches_the_client(self):
        client = self.FakeClient()
        provider = AnthropicProvider("claude-opus-5-5", "high", 1000, client=client)
        for objective in (f"token={FAKE_ASSIGNED}", f"{FAKE_ANTHROPIC} is the key", f"password='{FAKE_ASSIGNED} x y'"):
            with self.assertRaises(ProviderError) as caught:
                provider.propose_plan(self.request(objective))
            self.assertEqual(caught.exception.code, "secret_in_request")
        self.assertEqual(client.calls, [], "a request containing a credential was sent")

    def test_file_contents_are_redacted_in_the_outgoing_request(self):
        client = self.FakeClient()
        provider = AnthropicProvider("claude-opus-5-5", "high", 1000, client=client)
        files = {"calc/config.py": f"API_KEY = '{FAKE_ANTHROPIC}'\nPASSWORD = 'two words {FAKE_ASSIGNED}'\n"}
        with self.assertRaises(ProviderError):
            provider.propose_plan(self.request(files=files))
        self.assertEqual(len(client.calls), 1)
        sent = json.dumps(client.calls[0])
        for secret in (FAKE_ANTHROPIC, FAKE_ASSIGNED, "two words"):
            self.assertNotIn(secret, sent)
        self.assertIn("Fix the median bug", sent)   # valid, non-sensitive instructions still go out

    def test_failures_keep_codes_only(self):
        client = self.FakeClient()
        provider = AnthropicProvider("claude-opus-5-5", "high", 1000, client=client)
        with self.assertRaises(ProviderError) as caught:
            provider.propose_plan(self.request())
        self.assertEqual(caught.exception.code, "api_error:RuntimeError")
        self.assertNotIn("captured", str(caught.exception))

    def test_engine_path_end_to_end_with_capturing_provider(self):
        (self.source / "calc" / "settings.py").write_text(f'PASSWORD = "{FAKE_ASSIGNED} spaced"\n')
        mission_id = self.engine.create(real_provider_spec(self.source))
        self.engine.plan(mission_id, allow_real_model=True)
        sent = json.dumps(self.provider.requests[0].files)
        self.assertNotIn(FAKE_ASSIGNED, sent)
        self.assertNotIn("spaced", sent)


def _capture(fn):
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn()
    return (code, out.getvalue(), err.getvalue()), None


if __name__ == "__main__":
    unittest.main()
