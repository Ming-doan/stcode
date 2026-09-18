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
import contextlib
import logging
import os
import random
from typing import Any, AsyncGenerator, Literal, Mapping, TypeVar

import anthropic
import openai
from google.genai import errors as genai_errors
from pydantic import BaseModel

from stcode.core.common import trace
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.registry import get_provider
from stcode.core.providers.types import (
    Message,
    MessageStop,
    ReasoningEffort,
    StreamEvent,
    ToolDefinition,
)

logger = logging.getLogger("stcode.providers.gateway")

Difficulty = Literal["low", "medium", "high"]


class ProviderConfig(BaseModel):
    """A provider's main credentials — the fallback for any tier that does not override.

    `api_key_env` names an environment variable read at request time; `api_key` is a
    literal for people who would rather keep it in the file. The env var wins when set.

    `base_url`/`base_url_env` point at anything speaking the provider's wire protocol
    that is not the vendor's endpoint, under the same env-wins rule.
    """

    api_key_env: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    max_concurrent: int = 0
    """How many requests this endpoint will take at once. 0 means no cap.

    The cap belongs to the *endpoint*, not to an agent: one daemon shares one gateway
    across every session, every sub-agent and the supervisor, so five parallel `task`
    calls are five simultaneous completions against the same server. A hosted API
    absorbs that. A local one serving a 27b model on one GPU does not — it queues them
    and then drops the ones that waited too long, which reaches the agent as a 503 the
    retry policy cannot fix because nothing was transient about it.

    Set it to 1 for Ollama or llama.cpp and the same five calls run one after another.
    """


class RouteConfig(BaseModel):
    """A difficulty tier's target model, with optional credential overrides.

    Unset, the tier falls back to its provider's main config. Set them only when a tier
    needs different credentials or endpoint — a higher-quota key reserved for `high`.
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


_NO_LIMIT = ProviderConfig()
"""Stand-in for a provider with no entry in `[providers]` — routing may still name it
through a tier's own credentials, and an absent section is not a cap of zero."""

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _coerce(model: type[_ModelT], value: Any) -> _ModelT:
    """Accept either the model itself or a plain dict of the same shape."""
    return value if isinstance(value, model) else model.model_validate(value)


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

    def __init__(
        self,
        providers: Mapping[str, ProviderConfig | dict[str, Any]],
        routing: Mapping[Difficulty, RouteConfig | dict[str, Any]],
        retry: RetryConfig | dict[str, Any] | None = None,
    ) -> None:
        """Each argument takes the model instance or a plain dict of the same shape,
        coerced here once so a malformed entry fails loudly at construction rather than
        as an `AttributeError` inside `stream()` the first time that route is used.

        `Mapping`, not `dict`: both are copied and never mutated, and an invariant
        `dict[str, ProviderConfig | dict]` rejects the `dict[str, ProviderConfig]` that
        `GatewayConfig` actually holds.
        """
        self._providers_cfg = {
            name: _coerce(ProviderConfig, cfg) for name, cfg in providers.items()
        }
        self._routing = {
            difficulty: _coerce(RouteConfig, route) for difficulty, route in routing.items()
        }
        self._retry = _coerce(RetryConfig, retry) if retry is not None else RetryConfig()
        self._provider_instances: dict[tuple[str, str | None, str | None], BaseModelProvider] = {}
        self._limits: dict[tuple[str, int], asyncio.Semaphore] = {}
        """One semaphore per `(provider, cap)`, shared by everything holding this
        gateway — which is the point. Keyed by the cap as well as the name so lowering
        it in `reconfigure` makes a new, smaller one rather than a stale wide one."""

    async def reconfigure(
        self,
        *,
        providers: Mapping[str, ProviderConfig | dict[str, Any]],
        routing: Mapping[Difficulty, RouteConfig | dict[str, Any]],
        retry: RetryConfig | dict[str, Any] | None = None,
    ) -> None:
        """Replace this gateway's configuration **in place**, keeping its identity.

        In place because every agent in the daemon holds a reference to this object: a
        new key or base URL typed into `/model` has to reach the session already
        running, and swapping the daemon's gateway for a fresh one would leave that
        session streaming against the old credentials until it ended.

        The cached clients are closed and dropped, since a client is built around the
        key and base URL that are being replaced.
        """
        await self._close_clients()
        self._providers_cfg = {
            name: _coerce(ProviderConfig, cfg) for name, cfg in providers.items()
        }
        self._routing = {
            difficulty: _coerce(RouteConfig, route) for difficulty, route in routing.items()
        }
        if retry is not None:
            self._retry = _coerce(RetryConfig, retry)

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

        # A tier's credentials win over the provider's; within either, env beats literal.
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
        """The route for a tier, falling back to another when it has none.

        One absent line of TOML is not a reason to refuse to work. `medium` first, as
        the least wrong answer in either direction; only a gateway with no routes at all
        raises. This is config fallback, not provider failover.
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

    def _limiter(self, provider: str) -> "asyncio.Semaphore | contextlib.AbstractAsyncContextManager[Any]":
        """The in-flight cap for one provider, or a no-op when it has none.

        Built lazily rather than in `__init__`: a gateway is routinely constructed
        outside a running loop (the daemon builds one before it binds), and a semaphore
        is only ever awaited from inside one.
        """
        cap = self._providers_cfg.get(provider, _NO_LIMIT).max_concurrent
        if cap <= 0:
            return contextlib.nullcontext()
        key = (provider, cap)
        limiter = self._limits.get(key)
        if limiter is None:
            limiter = asyncio.Semaphore(cap)
            self._limits[key] = limiter
        return limiter

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
        given explicitly — which bypasses tier routing and uses that provider's main
        credentials.

        `reasoning_effort`, `temperature`, `top_p`, `stop` and `parallel_tool_calls`
        pass straight through; see `BaseModelProvider.stream` for the unified semantics.
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
                    yield event

    async def _stream_with_retry(
        self,
        provider_instance: BaseModelProvider,
        messages: list[Message],
        recorder: "trace.Recorder",
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """The retry loop. Split out only so the span above can wrap it whole."""
        retry = self._retry
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
