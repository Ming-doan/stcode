"""
LLM Gateway — model orchestration: difficulty-based routing and retry.

This is a thin dispatch layer: given a difficulty tier (or an explicit provider/model
override), it resolves which provider+model+credentials to use, retries transient
failures, and streams back unified StreamEvents. It deliberately does not run an agent
loop (no tool execution, no dynamic tool sets, no human-in-the-loop gating) — see
`LLMGateway`'s docstring for why.

Fully self-contained within `core/providers/`: `ProviderConfig`/`RouteConfig`/
`RetryConfig` are the gateway's own domain vocabulary (a provider's credentials, a
tier's route, the retry policy), not a fact about *files*. `LLMGateway` takes that data
directly and never touches paths, TOML, or `.env` — `core/configs.py` is the one place
that knows how the on-disk config maps onto these shapes and loads them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import AsyncGenerator, Literal

import anthropic
import openai
from google.genai import errors as genai_errors
from pydantic import BaseModel

from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.registry import get_provider
from stcode.core.providers.types import Message, ReasoningEffort, StreamEvent, ToolDefinition

logger = logging.getLogger("stcode.providers.gateway")

Difficulty = Literal["low", "medium", "high"]


class ProviderConfig(BaseModel):
    """A provider's main credentials — the fallback for any tier that doesn't override them.

    `api_key_env` names an environment variable to read the key from at request time;
    `api_key` is a literal fallback for people who'd rather keep it in the config file.
    The env var wins when both are set and the variable is actually present.

    `base_url`/`base_url_env` are for talking to anything that speaks the provider's wire
    protocol but isn't the vendor's own endpoint (a proxy, router, or self-hosted gateway),
    and follow the same env-wins-over-literal rule.
    """

    api_key_env: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None


class RouteConfig(BaseModel):
    """A difficulty tier's target model, with optional credential overrides.

    `api_key_env`/`api_key`/`base_url`/`base_url_env` are optional: when unset, the tier
    falls back to its provider's main config above — set them only when a tier needs
    different credentials or a different endpoint (e.g. a higher-quota key reserved
    for `high`).
    """

    provider: str
    model: str
    api_key_env: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None


class RetryConfig(BaseModel):
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 20.0
    jitter: bool = True


def resolve_secret(env_name: str | None, literal: str | None) -> str | None:
    """An env-var name wins over a literal value when the variable is actually set —
    shared resolution rule for both api keys and base URLs."""
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return literal


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

    def __init__(
        self,
        providers: dict[str, ProviderConfig | dict],
        routing: dict[Difficulty, RouteConfig | dict],
        retry: RetryConfig | dict | None = None,
    ) -> None:
        """`providers`/`routing`/`retry` accept either the model instances themselves or
        plain dicts of the same shape (e.g. inline literals in a script) — each is
        validated/coerced here, once, so a malformed entry fails loudly at construction
        with a clear pydantic error instead of an `AttributeError` deep inside `stream()`
        the first time that route is actually used.
        """
        self._providers_cfg = {
            name: cfg if isinstance(cfg, ProviderConfig) else ProviderConfig.model_validate(cfg)
            for name, cfg in providers.items()
        }
        self._routing = {
            difficulty: route if isinstance(route, RouteConfig) else RouteConfig.model_validate(route)
            for difficulty, route in routing.items()
        }
        self._retry = (
            retry
            if isinstance(retry, RetryConfig)
            else RetryConfig.model_validate(retry)
            if retry is not None
            else RetryConfig()
        )
        self._provider_instances: dict[tuple[str, str | None, str | None], BaseModelProvider] = {}

    async def aclose(self) -> None:
        """Close every cached provider client. Instances are cached and reused across
        calls to `stream()` (including retries) — never close one mid-stream or
        mid-retry; call this only once the gateway itself is being torn down."""
        for instance in self._provider_instances.values():
            await instance.aclose()
        self._provider_instances.clear()

    async def __aenter__(self) -> "LLMGateway":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    def _get_provider(
        self,
        name: str,
        api_key_env: str | None,
        api_key: str | None,
        base_url: str | None,
    ) -> BaseModelProvider:
        try:
            provider_cfg = self._providers_cfg[name]
        except KeyError:
            raise ValueError(
                f"Provider {name!r} not configured. Configured: {sorted(self._providers_cfg)}"
            ) from None

        # A tier's own credentials win over the provider's; within either, an env var
        # wins over a literal (see resolve_secret above).
        resolved_key = resolve_secret(
            api_key_env or provider_cfg.api_key_env, api_key or provider_cfg.api_key
        )
        resolved_base_url = base_url or resolve_secret(provider_cfg.base_url_env, provider_cfg.base_url)
        cache_key = (name, resolved_key, resolved_base_url)

        if cache_key not in self._provider_instances:
            self._provider_instances[cache_key] = get_provider(
                name, api_key=resolved_key, base_url=resolved_base_url
            )
        return self._provider_instances[cache_key]

    def _resolve_route(self, difficulty: Difficulty) -> RouteConfig:
        """The route for a tier, falling back to another tier when it has none.

        A missing `[routing.high]` used to raise and take down the turn. One absent
        line of TOML is not a reason to refuse to work: `medium` first because it is
        the least wrong answer in either direction, then whichever tier exists. Only a
        gateway with no routes at all still raises.

        This is config fallback, not provider failover — switching providers because
        one is down only means something when you pay for two, and it would go around
        the retry loop rather than here (EXPECTED.md §5.1).
        """
        route = self._routing.get(difficulty)
        if route is not None:
            return route
        for candidate in ("medium", "high", "low"):
            fallback = self._routing.get(candidate)  # type: ignore[arg-type]
            if fallback is not None:
                logger.warning(
                    "no route configured for difficulty %r; using %r (%s/%s)",
                    difficulty, candidate, fallback.provider, fallback.model,
                )
                return fallback
        raise ValueError(
            "No routes configured at all — every difficulty tier is missing. Add a "
            "[routing.medium] section to your config."
        )

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
        reasoning_effort: ReasoningEffort | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream one completion, routed by `difficulty` unless `provider`+`model` are
        given explicitly (which bypasses tier routing and uses that provider's main
        credentials).

        `reasoning_effort`, `temperature`, `top_p`, `stop`, and
        `parallel_tool_calls` pass straight through to the routed provider's
        `stream()` — see `BaseModelProvider.stream` for the unified semantics
        and each provider's own docstring for its mapping/caveats.
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

        retry = self._retry
        for attempt in range(1, retry.max_attempts + 1):
            started = False
            try:
                async for event in provider_instance.stream(
                    messages,
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
