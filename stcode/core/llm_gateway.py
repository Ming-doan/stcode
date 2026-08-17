"""
LLM Gateway — model orchestration: difficulty-based routing and retry.

This is a thin dispatch layer: given a difficulty tier (or an explicit provider/model
override), it resolves which provider+model+credentials to use, retries transient
failures, and streams back unified StreamEvents. It deliberately does not run an agent
loop (no tool execution, no dynamic tool sets, no human-in-the-loop gating) — see
`LLMGateway`'s docstring for why.

Config (where it lives, how it's structured, .env loading) is owned by `configs.py` —
this module only consumes the resulting `GatewayConfig`.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import AsyncGenerator

import anthropic
import openai
from google.genai import errors as genai_errors

from stcode.core.configs import Difficulty, GatewayConfig, RouteConfig, load_config, resolve_secret
from stcode.core.providers import BaseModelProvider, get_provider
from stcode.core.providers.types import Message, StreamEvent, ToolDefinition

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
    """Routes completions to a provider+model by difficulty tier, with retry on transient
    failures.

    Deliberately excludes the agent loop. Tool execution, dynamically changing the tool
    set turn-by-turn, and human-in-the-loop approval are all orchestration-level concerns
    that live *above* this layer and vary per call — baking a loop in here would mean
    either re-exposing every provider-specific knob through the gateway (defeating the
    point of the abstraction) or hard-coding one harness's shape into what should stay a
    thin "send this, stream that back" API. Callers own their loop and call `stream()`
    once per turn with whatever messages/tools that turn needs.
    """

    def __init__(self, config: GatewayConfig | None = None, config_path: Path | None = None) -> None:
        self._config = config or load_config(config_path)
        self._providers: dict[tuple[str, str | None, str | None], BaseModelProvider] = {}

    def _get_provider(
        self,
        name: str,
        api_key_env: str | None,
        api_key: str | None,
        base_url: str | None,
    ) -> BaseModelProvider:
        try:
            provider_cfg = self._config.providers[name]
        except KeyError:
            raise ValueError(
                f"Provider {name!r} not configured. Configured: {sorted(self._config.providers)}"
            ) from None

        # A tier's own credentials win over the provider's; within either, an env var
        # wins over a literal (see configs.resolve_secret).
        resolved_key = resolve_secret(
            api_key_env or provider_cfg.api_key_env, api_key or provider_cfg.api_key
        )
        resolved_base_url = base_url or resolve_secret(provider_cfg.base_url_env, provider_cfg.base_url)
        cache_key = (name, resolved_key, resolved_base_url)

        if cache_key not in self._providers:
            self._providers[cache_key] = get_provider(name, api_key=resolved_key, base_url=resolved_base_url)
        return self._providers[cache_key]

    def _resolve_route(self, difficulty: Difficulty) -> RouteConfig:
        try:
            return self._config.routing[difficulty]
        except KeyError:
            raise ValueError(
                f"No route configured for difficulty {difficulty!r}. "
                f"Configured: {sorted(self._config.routing)}"
            ) from None

    async def list_models(self, provider: str) -> list[str]:
        return await self._get_provider(provider, None, None, None).list_models()

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
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream one completion, routed by `difficulty` unless `provider`+`model` are
        given explicitly (which bypasses tier routing and uses that provider's main
        credentials).
        """
        if provider and model:
            provider_name, model_name = provider, model
            provider_instance = self._get_provider(provider_name, None, None, None)
        else:
            route = self._resolve_route(difficulty)
            provider_name = provider or route.provider
            model_name = model or route.model
            resolved_base_url = resolve_secret(route.base_url_env, route.base_url)
            provider_instance = self._get_provider(
                provider_name, route.api_key_env, route.api_key, resolved_base_url
            )

        retry = self._config.retry
        for attempt in range(1, retry.max_attempts + 1):
            started = False
            try:
                async for event in provider_instance.stream(
                    messages,
                    model=model_name,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                ):
                    started = True
                    yield event
                return
            except Exception as exc:
                # Once we've yielded output, the caller already has partial content —
                # retrying from scratch would duplicate it, so only retry failures that
                # happen before the stream produces anything.
                if started or attempt == retry.max_attempts or not _is_retryable(exc):
                    raise
                delay = min(retry.base_delay * (2 ** (attempt - 1)), retry.max_delay)
                if retry.jitter:
                    delay += random.uniform(0, delay * 0.1)
                await asyncio.sleep(delay)
