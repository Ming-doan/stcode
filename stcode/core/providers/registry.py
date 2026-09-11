"""
Provider registry — the LLM backends stcode can talk to, plus the static facts callers
need *before* instantiating one (default model, conventional key env var).

That metadata lives here so there is one place to edit when a provider is added, and so
a default model cannot drift from the provider module's own `DEFAULT_MODEL`.
"""

from dataclasses import dataclass

from stcode.core.providers.anthropic_claude import (
    DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL,
)
from stcode.core.providers.anthropic_claude import AnthropicProvider
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.google_gemini import DEFAULT_MODEL as GOOGLE_DEFAULT_MODEL
from stcode.core.providers.google_gemini import GoogleGenAIProvider
from stcode.core.providers.openai_gpt import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from stcode.core.providers.openai_gpt import OpenAIProvider

PROVIDERS: dict[str, type[BaseModelProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleGenAIProvider,
}


@dataclass(frozen=True)
class ProviderInfo:
    """Static description of a provider — no credentials, no client, no I/O."""

    default_model: str
    key_env: str


PROVIDER_INFO: dict[str, ProviderInfo] = {
    "anthropic": ProviderInfo(ANTHROPIC_DEFAULT_MODEL, "ANTHROPIC_API_KEY"),
    "openai": ProviderInfo(OPENAI_DEFAULT_MODEL, "OPENAI_API_KEY"),
    "google": ProviderInfo(GOOGLE_DEFAULT_MODEL, "GEMINI_API_KEY"),
}


_UNKNOWN = ProviderInfo(default_model="", key_env="")


def default_model_for(provider: str) -> str:
    """The model a provider is pointed at when the user hasn't picked one."""
    return PROVIDER_INFO.get(provider, _UNKNOWN).default_model


def key_env_for(provider: str) -> str:
    """The environment variable a provider's key conventionally lives in."""
    return PROVIDER_INFO.get(provider, _UNKNOWN).key_env


def get_provider(
    name: str, api_key: str | None = None, base_url: str | None = None
) -> BaseModelProvider:
    """Instantiate a registered provider by name (e.g. "anthropic", "openai", "google")."""
    try:
        provider_cls = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}. Available: {sorted(PROVIDERS)}") from None
    return provider_cls(api_key=api_key, base_url=base_url)
