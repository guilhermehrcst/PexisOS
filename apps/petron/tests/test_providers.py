"""The Anthropic provider is tested with a fake client only. No network, no credentials."""

import os
import unittest
from types import SimpleNamespace

from petron.providers import ProviderError
from petron.providers.anthropic_provider import AnthropicProvider
from petron.providers.base import PlanRequest

REQUEST = PlanRequest("m", "fix it", {"read_file": {"description": "d", "effect": "read",
                                                    "input_schema": {"type": "object"}}},
                      {"calc/stats.py": "x"}, ("calc/stats.py",), ("tests",), 4)


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def client(response=None, error=None):
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(content, stop_reason="tool_use"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5), _request_id="req_test")


def tool_use(name, data):
    return SimpleNamespace(type="tool_use", name=name, input=data)


class AnthropicProviderTests(unittest.TestCase):
    def provider(self, fake):
        return AnthropicProvider("claude-opus-5-5", "high", 16000, client=fake)

    def test_maps_submitted_plan_and_records_served_model(self):
        fake, messages = client(response([tool_use("submit_plan", {"rationale": "r", "steps": [
            {"tool": "read_file", "args": {"path": "calc/stats.py"}}]})]))
        proposal = self.provider(fake).propose_plan(REQUEST)
        self.assertEqual(proposal.plan["schema"], "petron.plan.v0")
        self.assertFalse(proposal.info["simulated"])
        self.assertEqual(proposal.info["model"], "claude-opus-5-5")
        self.assertEqual(messages.kwargs["tool_choice"], {"type": "auto"})
        self.assertEqual(messages.kwargs["fallbacks"], "default")
        self.assertEqual(messages.kwargs["tools"][0]["input_schema"]["properties"]["steps"]["items"]
                         ["properties"]["tool"]["enum"], ["read_file"])

    def test_refusal_truncation_and_missing_plan_are_errors(self):
        cases = [(response([], "refusal"), "model_refusal"), (response([], "max_tokens"), "model_truncated"),
                 (response([SimpleNamespace(type="text", text="hi")], "end_turn"), "no_plan_submitted")]
        for resp, code in cases:
            fake, _ = client(resp)
            with self.assertRaises(ProviderError) as caught:
                self.provider(fake).propose_plan(REQUEST)
            self.assertEqual(caught.exception.code, code)

    def test_sdk_errors_keep_only_the_class_name(self):
        fake, _ = client(error=RuntimeError("secret sk-ant-api03-zzzzzzzzzzzz in message"))
        with self.assertRaises(ProviderError) as caught:
            self.provider(fake).propose_plan(REQUEST)
        self.assertEqual(caught.exception.code, "api_error:RuntimeError")

    def test_missing_api_key_refused_before_any_import_or_call(self):
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(ProviderError) as caught:
                AnthropicProvider("claude-opus-5-5", "high", 16000).propose_plan(REQUEST)
            self.assertEqual(caught.exception.code, "api_key_missing")
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_base_url_is_explicit_not_inherited(self):
        provider = AnthropicProvider("claude-opus-5-5", "high", 16000)
        self.assertEqual(provider._base_url, "https://api.anthropic.com")


if __name__ == "__main__":
    unittest.main()
