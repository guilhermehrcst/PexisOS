"""Plan providers. ``build_provider`` is the only constructor the engine uses."""

from __future__ import annotations

from .base import PlanProposal, PlanRequest, Provider, ProviderError
from .deterministic import DeterministicProvider


_REAL_KEYS = ("model", "effort", "max_tokens")


def _bound_settings(config: dict) -> tuple:
    """A real provider is built only from settings the mission spec carries explicitly (the spec
    parser resolves defaults before approval). A missing value is refused, never defaulted here."""
    if any(key not in config for key in _REAL_KEYS):
        raise ProviderError("provider_settings_unbound")
    return config["model"], config["effort"], config["max_tokens"]


def build_provider(config: dict, *, allow_real_model: bool = False) -> Provider:
    kind = config["kind"]
    if kind == "deterministic":
        return DeterministicProvider(config["script"])
    if kind == "anthropic":
        if not allow_real_model:
            raise ProviderError("real_model_not_authorized")
        model, effort, max_tokens = _bound_settings(config)
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, effort=effort, max_tokens=max_tokens)
    raise ProviderError("unknown_provider")


def build_agent_provider(config: dict, *, allow_real_model: bool = False):
    """Provider for agent-mode missions (one decision per turn)."""
    kind = config["kind"]
    if kind == "deterministic":
        from .agent_scripted import ScriptedAgentProvider
        return ScriptedAgentProvider(config["script"])
    if kind == "anthropic":
        if not allow_real_model:
            raise ProviderError("real_model_not_authorized")
        model, effort, max_tokens = _bound_settings(config)
        from .anthropic_provider import AnthropicAgentProvider
        return AnthropicAgentProvider(model=model, effort=effort, max_tokens=max_tokens)
    raise ProviderError("unknown_provider")


__all__ = ["PlanProposal", "PlanRequest", "Provider", "ProviderError", "DeterministicProvider", "build_provider", "build_agent_provider"]
