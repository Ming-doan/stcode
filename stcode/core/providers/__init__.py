"""
Providers — LLM adapters, unified request/event types, the registry, and the
difficulty-routing gateway built on them.

A thin re-export hub; each concern is its own leaf module (`base.py`, `<provider>.py`,
`registry.py`, `gateway.py`) so nothing outside needs the internal layout.
"""

from stcode.core.providers.anthropic_claude import AnthropicProvider
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.gateway import LLMGateway
from stcode.core.providers.google_gemini import GoogleGenAIProvider
from stcode.core.providers.openai_gpt import OpenAIProvider
from stcode.core.providers.registry import (
    DEFAULT_MODELS,
    PROVIDERS,
    default_model_for,
    get_provider,
)

__all__ = [
    "DEFAULT_MODELS",
    "PROVIDERS",
    "AnthropicProvider",
    "BaseModelProvider",
    "GoogleGenAIProvider",
    "LLMGateway",
    "OpenAIProvider",
    "default_model_for",
    "get_provider",
]
