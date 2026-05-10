"""LLM provider factory."""
from __future__ import annotations

from mas_agent.providers.base import BaseLLMProvider


def create_provider(
    provider: str,
    model: str,
    provider_url: str | None = None,
    provider_key: str | None = None,
) -> BaseLLMProvider:
    from mas_agent.providers.anthropic_provider import AnthropicProvider

    # All providers (mimo, glm, etc.) use the Anthropic-compatible API format
    return AnthropicProvider(
        model=model,
        api_key=provider_key or "",
        base_url=provider_url,
    )
