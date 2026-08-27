"""
Provider package — LLM adapters, unified request/event types, the provider registry,
and the difficulty-routing gateway built on top of them.

This `__init__` is a thin re-export hub; each concern lives in its own leaf module
(`base.py`, `<provider>.py`, `registry.py`, `gateway.py`) so nothing outside this
package needs to know the internal layout.
"""

from stcode.core.providers.anthropic_claude import AnthropicProvider
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.gateway import (
    Difficulty,
    LLMGateway,
    ProviderConfig,
    RetryConfig,
    RouteConfig,
    resolve_secret,
)
from stcode.core.providers.google_gemini import GoogleGenAIProvider
from stcode.core.providers.openai_gpt import OpenAIProvider
from stcode.core.providers.registry import (
    PROVIDER_INFO,
    PROVIDERS,
    ProviderInfo,
    default_model_for,
    get_provider,
    key_env_for,
)

__all__ = [
    "PROVIDERS",
    "PROVIDER_INFO",
    "AnthropicProvider",
    "BaseModelProvider",
    "Difficulty",
    "GoogleGenAIProvider",
    "LLMGateway",
    "OpenAIProvider",
    "ProviderConfig",
    "ProviderInfo",
    "RetryConfig",
    "RouteConfig",
    "default_model_for",
    "get_provider",
    "key_env_for",
    "resolve_secret",
]
