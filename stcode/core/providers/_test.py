"""
Test does LLM Provider works
"""

import os

import anthropic
import httpx2
import openai
import pytest
from dotenv import load_dotenv
from google.genai import errors as genai_errors

import stcode.core.providers.gateway as gateway_module
from stcode.core.providers import (
    PROVIDER_INFO,
    PROVIDERS,
    AnthropicProvider,
    GoogleGenAIProvider,
    LLMGateway,
    OpenAIProvider,
    ProviderConfig,
    RetryConfig,
    RouteConfig,
    default_model_for,
    get_provider,
    key_env_for,
    resolve_secret,
)
from stcode.core.providers.anthropic_claude import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.google_gemini import DEFAULT_MODEL as GOOGLE_DEFAULT_MODEL
from stcode.core.providers.openai_gpt import DEFAULT_MODEL as OPENAI_DEFAULT_MODEL
from stcode.core.providers.types import (
    Message,
    MessageStop,
    TextDelta,
    ToolCallEnd,
    ToolDefinition,
    Usage,
)

load_dotenv()

pytestmark = pytest.mark.anyio

EXPECTED_CLASSES = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleGenAIProvider,
}

EXPECTED_DEFAULT_MODELS = {
    "anthropic": ANTHROPIC_DEFAULT_MODEL,
    "openai": OPENAI_DEFAULT_MODEL,
    "google": GOOGLE_DEFAULT_MODEL,
}

EXPECTED_KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
}

PROMPT = "Reply with exactly one word: ok"

GREET_TOOL = ToolDefinition(
    name="greet_user",
    description="Greet a person by name. Always call this when asked to greet someone.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The person's name"}},
        "required": ["name"],
    },
)

TOOL_PROMPT = "Use the greet_user tool to greet Alice. You must call the tool, not just reply in text."


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------- registry


def test_registry_has_exactly_the_known_providers():
    assert set(PROVIDERS) == set(EXPECTED_CLASSES)
    assert set(PROVIDER_INFO) == set(EXPECTED_CLASSES)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_registered_class_matches_expected(name):
    assert PROVIDERS[name] is EXPECTED_CLASSES[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_provider_info_matches_module_defaults(name):
    info = PROVIDER_INFO[name]
    assert info.default_model == EXPECTED_DEFAULT_MODELS[name]
    assert info.key_env == EXPECTED_KEY_ENVS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_default_model_for_and_key_env_for(name):
    assert default_model_for(name) == EXPECTED_DEFAULT_MODELS[name]
    assert key_env_for(name) == EXPECTED_KEY_ENVS[name]


def test_default_model_for_and_key_env_for_unknown_provider():
    assert default_model_for("does-not-exist") == ""
    assert key_env_for("does-not-exist") == ""


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_get_provider_instantiates_expected_class(name):
    provider = get_provider(name, api_key="dummy-key", base_url="https://example.invalid")
    assert isinstance(provider, EXPECTED_CLASSES[name])
    assert provider.api_key == "dummy-key"
    assert provider.base_url == "https://example.invalid"


def test_get_provider_unknown_name_raises_value_error():
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("does-not-exist")


# --------------------------------------------------------------- single provider (live)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
async def test_provider_streams_a_reply(name):
    """Live connectivity check — skipped for any provider without a key in the
    environment (process env or .env), so this passes on a fresh checkout and only
    exercises whichever providers are actually configured."""
    info = PROVIDER_INFO[name]
    api_key = os.environ.get(info.key_env)
    if not api_key:
        pytest.skip(f"{info.key_env} not set")

    text = ""
    stop_event = None
    async with get_provider(
        name, api_key=api_key, base_url=os.environ.get(f"{name.upper()}_BASE_URL")
    ) as provider:
        async for event in provider.stream(
            [Message(role="user", content=PROMPT)],
            model=default_model_for(name),
            max_tokens=16,
        ):
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, MessageStop):
                stop_event = event

    assert stop_event is not None
    assert text.strip()


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
async def test_provider_calls_a_tool(name):
    """Live connectivity check for the tool-calling path — same skip rule as
    test_provider_streams_a_reply. Exercises ToolCallStart/Delta/End end-to-end
    against each real provider's wire format, not just the text path."""
    info = PROVIDER_INFO[name]
    api_key = os.environ.get(info.key_env)
    if not api_key:
        pytest.skip(f"{info.key_env} not set")

    tool_call_end: ToolCallEnd | None = None
    stop_event: MessageStop | None = None
    async with get_provider(
        name, api_key=api_key, base_url=os.environ.get(f"{name.upper()}_BASE_URL")
    ) as provider:
        async for event in provider.stream(
            [Message(role="user", content=TOOL_PROMPT)],
            model=default_model_for(name),
            tools=[GREET_TOOL],
            max_tokens=256,
        ):
            if isinstance(event, ToolCallEnd):
                tool_call_end = event
            elif isinstance(event, MessageStop):
                stop_event = event

    assert tool_call_end is not None
    assert tool_call_end.name == "greet_user"
    assert "alice" in str(tool_call_end.input).lower()
    assert stop_event is not None
    assert stop_event.stop_reason == "tool_use"


# --------------------------------------------------------------------- resolve_secret


def test_resolve_secret_env_wins_over_literal(monkeypatch):
    monkeypatch.setenv("STCODE_TEST_KEY", "from-env")
    assert resolve_secret("STCODE_TEST_KEY", "from-literal") == "from-env"


def test_resolve_secret_falls_back_to_literal_when_env_unset(monkeypatch):
    monkeypatch.delenv("STCODE_TEST_KEY", raising=False)
    assert resolve_secret("STCODE_TEST_KEY", "from-literal") == "from-literal"


def test_resolve_secret_no_env_name_uses_literal():
    assert resolve_secret(None, "from-literal") == "from-literal"


# --------------------------------------------------------------------- _is_retryable


def test_is_retryable_classifies_known_transient_errors():
    req = httpx2.Request("POST", "http://example.invalid")
    assert gateway_module._is_retryable(anthropic.APIConnectionError(request=req))
    assert gateway_module._is_retryable(openai.APIConnectionError(request=req))
    assert gateway_module._is_retryable(genai_errors.ServerError(code=503, response_json={}))
    assert gateway_module._is_retryable(genai_errors.ClientError(code=429, response_json={}))


def test_is_retryable_rejects_non_transient_errors():
    assert not gateway_module._is_retryable(genai_errors.ClientError(code=400, response_json={}))
    assert not gateway_module._is_retryable(ValueError("bad request"))


# --------------------------------------------------------------------- LLMGateway


class FakeProvider(BaseModelProvider):
    """Minimal BaseModelProvider double for gateway unit tests — no network. `fail_times`
    raises `error` on that many leading calls to `stream()` before yielding `events`."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        events: list | None = None,
        error: Exception | None = None,
        fail_times: int = 0,
    ) -> None:
        super().__init__(api_key, base_url)
        self.events = events or []
        self.error = error
        self.fail_times = fail_times
        self.calls: list[dict] = []

    async def list_models(self) -> list[str]:
        return ["fake-model"]

    async def stream(
        self,
        messages,
        *,
        model,
        system=None,
        tools=None,
        max_tokens=8192,
        reasoning_effort=None,
        temperature=None,
        top_p=None,
        stop=None,
        parallel_tool_calls=None,
    ):
        self.calls.append(
            dict(
                model=model,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                top_p=top_p,
                stop=stop,
                parallel_tool_calls=parallel_tool_calls,
            )
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error or RuntimeError("transient")
        for event in self.events:
            yield event


def _install_fake_get_provider(monkeypatch, fake: BaseModelProvider) -> None:
    monkeypatch.setattr(gateway_module, "get_provider", lambda name, api_key=None, base_url=None: fake)


async def _drain(agen) -> list:
    return [event async for event in agen]


async def test_gateway_routes_by_difficulty_tier(monkeypatch):
    fake = FakeProvider(events=[MessageStop(stop_reason="end_turn", usage=Usage())])
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={
            "low": RouteConfig(provider="fake", model="fake-small"),
            "medium": RouteConfig(provider="fake", model="fake-medium"),
            "high": RouteConfig(provider="fake", model="fake-large"),
        },
    )
    events = await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="high"))
    assert fake.calls[-1]["model"] == "fake-large"
    assert isinstance(events[-1], MessageStop)


async def test_gateway_explicit_provider_and_model_bypasses_routing(monkeypatch):
    fake = FakeProvider(events=[MessageStop(stop_reason="end_turn", usage=Usage())])
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(providers={"fake": ProviderConfig(api_key="k")}, routing={})
    await _drain(
        gateway.stream(
            [Message(role="user", content="hi")], provider="fake", model="explicit-model"
        )
    )
    assert fake.calls[-1]["model"] == "explicit-model"


async def test_gateway_falls_back_to_a_configured_tier(monkeypatch):
    """One missing line of TOML must not kill the turn (EXPECTED.md §5.1)."""
    fake = FakeProvider(events=[MessageStop(stop_reason="end_turn", usage=Usage())])
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"medium": RouteConfig(provider="fake", model="fake-medium")},
    )
    await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="high"))
    assert fake.calls[-1]["model"] == "fake-medium"


async def test_gateway_with_no_routes_at_all_raises_value_error():
    gateway = LLMGateway(providers={}, routing={})
    with pytest.raises(ValueError, match="No routes configured at all"):
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))


async def test_gateway_unconfigured_provider_raises_value_error():
    gateway = LLMGateway(
        providers={}, routing={"low": RouteConfig(provider="missing", model="m")}
    )
    with pytest.raises(ValueError, match="not configured"):
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))


async def test_gateway_prefers_tier_credentials_over_provider_credentials(monkeypatch):
    captured: dict = {}

    def fake_get_provider(name, api_key=None, base_url=None):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return FakeProvider(events=[MessageStop(stop_reason="end_turn", usage=Usage())])

    monkeypatch.setattr(gateway_module, "get_provider", fake_get_provider)
    gateway = LLMGateway(
        providers={
            "fake": ProviderConfig(api_key="provider-level-key", base_url="https://provider.example")
        },
        routing={"low": RouteConfig(provider="fake", model="m", api_key="tier-level-key")},
    )
    await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert captured["api_key"] == "tier-level-key"
    assert captured["base_url"] == "https://provider.example"


async def test_gateway_forwards_generation_params_to_provider(monkeypatch):
    fake = FakeProvider(events=[MessageStop(stop_reason="end_turn", usage=Usage())])
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"low": RouteConfig(provider="fake", model="m")},
    )
    await _drain(
        gateway.stream(
            [Message(role="user", content="hi")],
            difficulty="low",
            reasoning_effort="high",
            temperature=0.2,
            top_p=0.9,
            stop=["END"],
            parallel_tool_calls=False,
        )
    )
    call = fake.calls[-1]
    assert call["reasoning_effort"] == "high"
    assert call["temperature"] == 0.2
    assert call["top_p"] == 0.9
    assert call["stop"] == ["END"]
    assert call["parallel_tool_calls"] is False


async def test_gateway_retries_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)
    fake = FakeProvider(fail_times=1, events=[MessageStop(stop_reason="end_turn", usage=Usage())])
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"low": RouteConfig(provider="fake", model="m")},
        retry=RetryConfig(max_attempts=3, base_delay=0, max_delay=0, jitter=False),
    )
    events = await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.calls) == 2
    assert isinstance(events[-1], MessageStop)


async def test_gateway_does_not_retry_non_transient_error(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: False)
    fake = FakeProvider(fail_times=1, error=ValueError("bad request"))
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"low": RouteConfig(provider="fake", model="m")},
    )
    with pytest.raises(ValueError, match="bad request"):
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.calls) == 1


async def test_gateway_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)
    fake = FakeProvider(fail_times=99, error=RuntimeError("still down"))
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"low": RouteConfig(provider="fake", model="m")},
        retry=RetryConfig(max_attempts=2, base_delay=0, max_delay=0, jitter=False),
    )
    with pytest.raises(RuntimeError, match="still down"):
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.calls) == 2


async def test_gateway_does_not_retry_after_partial_output(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)

    class PartialThenFailProvider(BaseModelProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_models(self) -> list[str]:
            return []

        async def stream(self, messages, *, model, system=None, tools=None, max_tokens=8192, **_):
            self.calls += 1
            yield TextDelta(text="partial")
            raise RuntimeError("dropped mid-stream")

    fake = PartialThenFailProvider()
    _install_fake_get_provider(monkeypatch, fake)
    gateway = LLMGateway(
        providers={"fake": ProviderConfig(api_key="k")},
        routing={"low": RouteConfig(provider="fake", model="m")},
    )
    received = []
    with pytest.raises(RuntimeError, match="dropped mid-stream"):
        async for event in gateway.stream([Message(role="user", content="hi")], difficulty="low"):
            received.append(event)
    assert len(received) == 1
    assert fake.calls == 1  # no retry attempted despite _is_retryable always returning True
