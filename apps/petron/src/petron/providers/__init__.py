"""Plan providers. ``build_provider`` is the only constructor the engine uses."""

from __future__ import annotations

from .base import PlanProposal, PlanRequest, Provider, ProviderError
from .deterministic import DeterministicProvider


def build_provider(config: dict, *, allow_real_model: bool = False) -> Provider:
    kind = config["kind"]
    if kind == "deterministic":
        return DeterministicProvider(config["script"])
    if kind == "anthropic":
        if not allow_real_model:
            raise ProviderError("real_model_not_authorized")
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=config.get("model", "claude-opus-5-5"), effort=config.get("effort", "high"),
                                 max_tokens=config.get("max_tokens", 16000))
    raise ProviderError("unknown_provider")


__all__ = ["PlanProposal", "PlanRequest", "Provider", "ProviderError", "DeterministicProvider", "build_provider"]
