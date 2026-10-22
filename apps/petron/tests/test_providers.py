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
    return SimpleNamespace(messages=messages), messages


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
        self.assertNotIn("fallbacks", messages.kwargs)  # no automatic model substitution
        self.assertNotIn("betas", messages.kwargs)
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


class ScriptedClient:
    """Fake Messages API returning queued responses and recording every request."""

    def __init__(self, responses):
        self.queue, self.requests = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        import copy
        self.requests.append(copy.deepcopy(kwargs))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def use(name, data, ident):
    return SimpleNamespace(type="tool_use", name=name, input=data, id=ident)


def turn_ctx(history=()):
    from petron.agent import AgentTurnContext
    tools = {"read_file": {"description": "d", "effect": "read", "input_schema": {"type": "object"}},
             "pexisos.list_entities": {"description": "d", "effect": "external_read",
                                       "input_schema": {"type": "object"}}}
    return AgentTurnContext("m", "fix it", tools, {"writable": ["a.py"]}, len(history), {"iterations": 5},
                            tuple(history))


class AnthropicAgentProviderTests(unittest.TestCase):
    def provider(self, responses):
        from petron.providers.anthropic_provider import AnthropicAgentProvider
        fake = ScriptedClient(responses)
        return AnthropicAgentProvider("claude-opus-5-5", "high", 16000, client=fake), fake

    def test_tool_use_maps_to_decision_and_observation_returns_as_tool_result(self):
        provider, fake = self.provider([response([use("read_file", {"path": "a.py"}, "toolu_1")]),
                                        response([use("petron_request_verification", {"summary": "s"}, "toolu_2")])])
        first = provider.next_decision(turn_ctx())
        self.assertEqual(first.decision, {"kind": "tool_call", "tool": "read_file", "args": {"path": "a.py"}})
        self.assertEqual(fake.requests[0]["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertNotIn("fallbacks", fake.requests[0])
        names = [t["name"] for t in fake.requests[0]["tools"]]
        self.assertIn("pexisos__list_entities", names)  # API-safe name, mapped back below
        observation = {"ok": False, "error_class": "test_failure", "result": {"exit_code": 1}}
        second = provider.next_decision(turn_ctx([{"iteration": 1, "decision": first.decision,
                                                    "observation": observation}]))
        self.assertEqual(second.decision["kind"], "request_verification")
        feedback = fake.requests[1]["messages"][-1]["content"][0]
        self.assertEqual(feedback["type"], "tool_result")
        self.assertEqual(feedback["tool_use_id"], "toolu_1")
        self.assertTrue(feedback["is_error"])
        self.assertIn("test_failure", feedback["content"])
        # assistant content is replayed unchanged (append-only conversation)
        self.assertEqual(fake.requests[1]["messages"][1]["role"], "assistant")

    def test_dotted_tool_names_map_back(self):
        provider, _ = self.provider([response([use("pexisos__list_entities", {}, "t1")])])
        self.assertEqual(provider.next_decision(turn_ctx()).decision["tool"], "pexisos.list_entities")

    def test_refusal_is_an_explicit_error_not_a_fallback(self):
        provider, fake = self.provider([response([], "refusal")])
        with self.assertRaises(ProviderError) as caught:
            provider.next_decision(turn_ctx())
        self.assertEqual(caught.exception.code, "model_refusal")
        self.assertEqual(len(fake.requests), 1)

    def test_reply_without_tool_call_becomes_invalid_decision(self):
        from petron.agent import DecisionInvalid, validate_decision
        provider, _ = self.provider([response([SimpleNamespace(type="text", text="I think it works")], "end_turn")])
        with self.assertRaises(DecisionInvalid):
            validate_decision(provider.next_decision(turn_ctx()).decision)

    def test_check_model_uses_models_api(self):
        from petron.providers.anthropic_provider import check_model
        fake = SimpleNamespace(models=SimpleNamespace(retrieve=lambda m: SimpleNamespace(id=m, display_name="X")))
        self.assertEqual(check_model("claude-opus-5-5", client=fake)["id"], "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
