"""
Provider registry — the LLM libraries stcode can talk to, and each one's default model.

The default model lives in each provider module and is read from there, so it cannot
drift. Which environment variable holds a library's key is configuration, and lives in
`core/configs.py` (`PROVIDER_KEY_ENV`).
"""

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


DEFAULT_MODELS: dict[str, str] = {
    "anthropic": ANTHROPIC_DEFAULT_MODEL,
    "openai": OPENAI_DEFAULT_MODEL,
    "google": GOOGLE_DEFAULT_MODEL,
}


def default_model_for(provider: str) -> str:
    """The model a library is pointed at when the config names none."""
    return DEFAULT_MODELS.get(provider, "")


def get_provider(
    name: str, api_key: str | None = None, base_url: str | None = None
) -> BaseModelProvider:
    """Instantiate a registered provider by name (e.g. "anthropic", "openai", "google")."""
    try:
        provider_cls = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown provider {name!r}. Available: {sorted(PROVIDERS)}") from None
    return provider_cls(api_key=api_key, base_url=base_url)
