"""
LLM Gateway — model orchestration: difficulty-based routing and retry.

This is a thin dispatch layer: given a difficulty tier (or an explicit provider/model
override), it resolves which provider+model+credentials to use, retries transient
failures, and streams back unified StreamEvents. It deliberately does not run an agent
loop (no tool execution, no dynamic tool sets, no human-in-the-loop gating) — see
`LLMGateway`'s docstring for why.

It takes a `ModelConfig` and asks it for routes and credentials; it never touches paths,
TOML or the environment itself — `core/configs.py` owns all three.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any, AsyncGenerator

import anthropic
import openai
from google.genai import errors as genai_errors

from stcode.core.common import trace
from stcode.core.configs import Difficulty, ModelConfig
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.registry import default_model_for, get_provider
from stcode.core.providers.types import (
    Message,
    MessageStop,
    ReasoningEffort,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolDefinition,
)

logger = logging.getLogger("stcode.providers.gateway")


# Transient/retryable errors per SDK — network hiccups, rate limits, and 5xx. Anything
# else (bad request, auth, not found, content policy) is a caller/config bug and should
# surface immediately rather than being retried.
_RETRYABLE_ANTHROPIC: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,  # also covers APITimeoutError, a subclass
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
)
_RETRYABLE_OPENAI: tuple[type[Exception], ...] = (
    openai.APIConnectionError,  # also covers APITimeoutError, a subclass
    openai.RateLimitError,
    openai.InternalServerError,
)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_ANTHROPIC + _RETRYABLE_OPENAI):
        return True
    if isinstance(exc, genai_errors.ServerError):
        return True
    # google-genai has no dedicated RateLimitError — 429 surfaces as a generic ClientError.
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return True
    return False


class LLMGateway:
    """Routes completions to a provider+model by difficulty tier, retrying transient
    failures.

    Deliberately excludes the agent loop. Tool execution, turn-by-turn tool sets and
    human-in-the-loop approval all vary per call and live *above* this layer; baking a
    loop in would mean either re-exposing every provider knob through the gateway or
    hard-coding one harness's shape into a thin "send this, stream that back" API.
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._provider_instances: dict[tuple[str, str | None, str | None], BaseModelProvider] = {}
        self._limits: dict[tuple[str, int], asyncio.Semaphore] = {}
        """One semaphore per `(provider, cap)`, shared by everything holding this
        gateway — which is the point. Keyed by the cap as well as the name so lowering
        it in `reconfigure` makes a new, smaller one rather than a stale wide one."""

    async def reconfigure(self, config: ModelConfig) -> None:
        """Replace this gateway's configuration **in place**, keeping its identity.

        In place because every agent in the daemon holds a reference to this object: a
        new key typed into `/model` has to reach the session already running. The
        cached clients are closed, since each was built around the key it replaces.
        """
        await self._close_clients()
        self._config = config

    async def aclose(self) -> None:
        """Close every cached provider client. They are reused across `stream()` calls
        and retries, so call this only when tearing the gateway down."""
        await self._close_clients()

    async def _close_clients(self) -> None:
        for instance in self._provider_instances.values():
            await instance.aclose()
        self._provider_instances.clear()

    async def __aenter__(self) -> "LLMGateway":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    def _get_provider(self, name: str) -> BaseModelProvider:
        """A client for one `[model.providers]` entry, cached by its credentials."""
        entry = self._config.providers[name]
        key, base_url = entry.key(), entry.url()
        cache_key = (name, key, base_url)
        if cache_key not in self._provider_instances:
            self._provider_instances[cache_key] = get_provider(
                entry.provider, api_key=key, base_url=base_url
            )
        return self._provider_instances[cache_key]

    def resolve(
        self, difficulty: Difficulty, provider: str | None = None, model: str | None = None
    ) -> tuple[str, str]:
        """`(provider entry, model)` for one call.

        A `provider` override (a session's `/model`) replaces the tier's route; a
        `model` override alone keeps the route's provider. An empty model means the
        library's own default.
        """
        if provider:
            name, routed = provider, ""
        else:
            name, routed = self._config.route(difficulty)
        entry = self._config.providers.get(name)
        if entry is None:
            raise ValueError(
                f"Provider {name!r} not configured. Configured: {sorted(self._config.providers)}"
            )
        return name, model or routed or entry.model or default_model_for(entry.provider)

    def _limiter(self, provider: str) -> "asyncio.Semaphore | contextlib.AbstractAsyncContextManager[Any]":
        """The in-flight cap for one provider, or a no-op when it has none.

        Built lazily rather than in `__init__`: a gateway is routinely constructed
        outside a running loop (the daemon builds one before it binds), and a semaphore
        is only ever awaited from inside one.
        """
        entry = self._config.providers.get(provider)
        cap = entry.max_concurrent if entry is not None else 0
        if cap <= 0:
            return contextlib.nullcontext()
        key = (provider, cap)
        limiter = self._limits.get(key)
        if limiter is None:
            limiter = asyncio.Semaphore(cap)
            self._limits[key] = limiter
        return limiter

    async def list_models(self, provider: str) -> list[str]:
        return await self._get_provider(self.resolve("medium", provider)[0]).list_models()

    async def stream(
        self,
        messages: list[Message],
        *,
        difficulty: Difficulty = "medium",
        provider: str | None = None,
        model: str | None = None,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        reasoning_effort: ReasoningEffort | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream one completion, routed by `difficulty` unless overridden. → `resolve`

        `reasoning_effort`, `temperature`, `top_p`, `stop` and `parallel_tool_calls`
        pass straight through; see `BaseModelProvider.stream` for the unified semantics.
        """
        provider_name, model_name = self.resolve(difficulty, provider, model)
        provider_instance = self._get_provider(provider_name)

        # `chat <model>`, the GenAI convention's name for an inference span. Platforms
        # read the `gen_ai.*` attributes off it and show a generation with its tokens;
        # anything named our own way shows as an anonymous box.
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider_name,
            "gen_ai.request.model": model_name,
            "gen_ai.request.max_tokens": max_tokens,
            "stcode.difficulty": difficulty,
        }
        # Held for the whole completion, not just the request: the point is how many
        # streams the endpoint is serving at once. Every caller wraps its iteration in
        # `aclosing`, so abandoning a stream still releases this.
        async with self._limiter(provider_name):
            with trace.span(f"chat {model_name}", kind="client", attributes=attributes) as recorder:
                capture = trace.records_content()
                output: list[dict[str, Any]] = []
                if capture:
                    inputs = [message.model_dump(mode="json") for message in messages]
                    if system:
                        inputs.insert(0, {"role": "system", "content": system})
                    recorder.set(**{"gen_ai.input.messages": inputs})
                async for event in self._stream_with_retry(
                    provider_instance,
                    messages,
                    recorder,
                    model=model_name,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    temperature=temperature,
                    top_p=top_p,
                    stop=stop,
                    parallel_tool_calls=parallel_tool_calls,
                ):
                    if capture and isinstance(event, (TextDelta, ToolCallEnd)):
                        output.append(event.model_dump(mode="json"))
                    yield event
                if capture:
                    recorder.set(**{"gen_ai.output.messages": output})

    async def _stream_with_retry(
        self,
        provider_instance: BaseModelProvider,
        messages: list[Message],
        recorder: "trace.Recorder",
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """The retry loop. Split out only so the span above can wrap it whole."""
        retry = self._config.retry
        for attempt in range(1, retry.max_attempts + 1):
            started = False
            try:
                async for event in provider_instance.stream(messages, **kwargs):
                    started = True
                    if isinstance(event, MessageStop):
                        recorder.set(
                            **{
                                "gen_ai.response.finish_reasons": [event.stop_reason],
                                "gen_ai.usage.input_tokens": event.usage.input_tokens,
                                "gen_ai.usage.output_tokens": event.usage.output_tokens,
                                "gen_ai.usage.cache_read_input_tokens": (
                                    event.usage.cache_read_input_tokens
                                ),
                            }
                        )
                    yield event
                return
            except Exception as exc:
                # Once anything is yielded the caller has partial content, and retrying
                # would duplicate it. Only pre-first-token failures are retryable.
                if started or attempt == retry.max_attempts or not _is_retryable(exc):
                    raise
                delay = min(retry.base_delay * (2 ** (attempt - 1)), retry.max_delay)
                if retry.jitter:
                    delay += random.uniform(0, delay * 0.1)
                await asyncio.sleep(delay)
