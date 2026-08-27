"""
Base LLM Provider Class
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator

from stcode.core.providers.types import Message, ReasoningEffort, StreamEvent, ToolDefinition


class BaseModelProvider(ABC):
    """Base LLM Provider Class — external LLM provider communication async methods."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """`base_url` lets a subclass talk to any endpoint that speaks its provider's
        wire protocol — an OpenAI-compatible router, a self-hosted Anthropic-compatible
        gateway, a proxy — not just the vendor's own API.
        """
        self.api_key = api_key
        self.base_url = base_url

    async def aclose(self) -> None:
        """Release the underlying SDK client's network resources (connection pool,
        open sockets). Subclasses whose client holds one should override this;
        callers should always await it — via `async with provider:` or a
        try/finally — once done streaming, or the client's transport can outlive
        the event loop and raise on interpreter shutdown.
        """

    async def __aenter__(self) -> "BaseModelProvider":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

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
        reasoning_effort: ReasoningEffort | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a completion as a sequence of unified StreamEvents.

        `messages` holds only user/assistant turns — the system prompt is
        passed separately since every provider treats it as a distinct field.

        `reasoning_effort` is the superset of every provider's effort scale
        (`"none"` through `"max"`); each provider maps it onto its own control
        (OpenAI's `reasoning_effort`, Anthropic's adaptive `thinking` +
        `output_config.effort`, Gemini's `thinking_config.thinking_level`) and
        clamps or drops values its API doesn't support. Leave it `None` to let
        the provider apply its own default rather than forcing a level.

        `temperature`, `top_p`, `stop`, and `parallel_tool_calls` are passed
        through to each provider's equivalent field as-is — this base class
        does not resolve cross-field conflicts a provider's own API rejects
        (e.g. Anthropic returns 400 for `temperature`/`top_p` alongside
        adaptive thinking on current-generation models); see each provider's
        `stream()` docstring for its specific caveats.
        """
