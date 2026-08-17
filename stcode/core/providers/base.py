"""
Base LLM Provider Class
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator

from stcode.core.providers.types import Message, StreamEvent, ToolDefinition


class BaseModelProvider(ABC):
    """Base LLM Provider Class — external LLM provider communication async methods."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """`base_url` lets a subclass talk to any endpoint that speaks its provider's
        wire protocol — an OpenAI-compatible router, a self-hosted Anthropic-compatible
        gateway, a proxy — not just the vendor's own API.
        """
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def list_models(self) -> list[str]:
        """List all possible models in provider."""

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a completion as a sequence of unified StreamEvents.

        `messages` holds only user/assistant turns — the system prompt is
        passed separately since every provider treats it as a distinct field.
        """
