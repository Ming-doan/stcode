"""
Test does LLM Provider works
"""

import asyncio
import contextlib
import os
from contextlib import aclosing

import anthropic
import httpx2
import openai
import pytest
from fakes import fake_provider, says
from dotenv import load_dotenv
from google.genai import errors as genai_errors

import stcode.core.providers.gateway as gateway_module
from stcode.core.configs import PROVIDER_KEY_ENV, ModelConfig
from stcode.core.providers import (
    DEFAULT_MODELS,
    PROVIDERS,
    AnthropicProvider,
    BaseModelProvider,
    GoogleGenAIProvider,
    LLMGateway,
    OpenAIProvider,
    default_model_for,
    get_provider,
)
from stcode.core.providers.anthropic_claude import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
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
    assert set(DEFAULT_MODELS) == set(EXPECTED_CLASSES)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_registered_class_matches_expected(name):
    assert PROVIDERS[name] is EXPECTED_CLASSES[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_default_model_for_matches_module_defaults(name):
    assert default_model_for(name) == EXPECTED_DEFAULT_MODELS[name]


def test_default_model_for_unknown_provider():
    assert default_model_for("does-not-exist") == ""


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
#
# These two are the only tests in the suite that leave the machine. They are marked
# `live` and deselected by default (`pytest.ini`): run them with `uv run pytest -m live`
# when you want to know whether a provider's wire format still matches this adapter.
#
# The skip-on-missing-key rule is kept on top of the marker, because a key being *set*
# is not the same as it being valid for the model this asks for.


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
async def test_provider_streams_a_reply(name):
    """Live connectivity check — skipped for any provider without a key in the
    environment (process env or .env)."""
    key_env = PROVIDER_KEY_ENV[name]
    api_key = os.environ.get(key_env)
    if not api_key:
        pytest.skip(f"{key_env} not set")

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


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
async def test_provider_calls_a_tool(name):
    """Live connectivity check for the tool-calling path — same skip rule as
    test_provider_streams_a_reply. Exercises ToolCallStart/Delta/End end-to-end
    against each real provider's wire format, not just the text path."""
    key_env = PROVIDER_KEY_ENV[name]
    api_key = os.environ.get(key_env)
    if not api_key:
        pytest.skip(f"{key_env} not set")

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


# The double and the registry swap both live in `tests/fakes.py`. `fake_provider`
# stands in for the `openai` library in `PROVIDERS`, so the gateway's own `_get_provider` — its
# credential resolution and its instance cache — runs for real; only the SDK is gone.


async def _drain(agen) -> list:
    return [event async for event in agen]


def _model_config(*, providers=None, routing=None, retry=None) -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "providers": providers or {"fake": {"provider": "openai", "api_key": "k"}},
            "routing": {"low": {"provider": "fake", "model": "m"}} if routing is None else routing,
            **({"retry": retry} if retry else {}),
        }
    )


def _gateway(**kwargs) -> LLMGateway:
    return LLMGateway(_model_config(**kwargs))


async def test_gateway_routes_by_difficulty_tier():
    with fake_provider() as fake:
        gateway = _gateway(
            routing={
                "low": {"provider": "fake", "model": "fake-small"},
                "medium": {"provider": "fake", "model": "fake-medium"},
                "high": {"provider": "fake", "model": "fake-large"},
            }
        )
        events = await _drain(
            gateway.stream([Message(role="user", content="hi")], difficulty="high")
        )
    assert fake.last.model == "fake-large"
    assert isinstance(events[-1], MessageStop)


async def test_gateway_explicit_provider_and_model_bypasses_routing():
    with fake_provider() as fake:
        gateway = _gateway(routing={})
        await _drain(
            gateway.stream(
                [Message(role="user", content="hi")], provider="fake", model="explicit-model"
            )
        )
    assert fake.last.model == "explicit-model"


async def test_a_tier_with_no_route_uses_the_default_entry():
    """One missing line of TOML must not kill the turn: the default entry answers, with
    the library's default model when the entry names none."""
    with fake_provider() as fake:
        gateway = _gateway(routing={"medium": {"provider": "fake", "model": "fake-medium"}})
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="high"))
    assert fake.last.model == OPENAI_DEFAULT_MODEL


async def test_gateway_with_no_provider_at_all_raises_value_error():
    gateway = LLMGateway(ModelConfig())
    with pytest.raises(ValueError, match="No provider configured"):
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))


async def test_a_session_override_naming_an_unknown_entry_raises():
    gateway = _gateway()
    with pytest.raises(ValueError, match="not configured"):
        await _drain(gateway.stream([Message(role="user", content="hi")], provider="missing"))


async def test_an_entry_key_and_base_url_reach_the_client(monkeypatch):
    """`${VAR}` resolves when the client is built, not when the config is read."""
    monkeypatch.setenv("STCODE_TEST_KEY", "from-env")
    with fake_provider() as fake:
        gateway = _gateway(
            providers={
                "fake": {
                    "provider": "openai",
                    "api_key": "${STCODE_TEST_KEY}",
                    "base_url": "https://provider.example",
                }
            }
        )
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert fake.credentials[-1] == ("from-env", "https://provider.example")


async def test_gateway_reuses_one_provider_instance_across_calls():
    """The gateway caches by (provider, key, base_url) so a daemon holding eight
    sessions holds one connection pool, not eight."""
    with fake_provider() as fake:
        gateway = _gateway()
        await _drain(gateway.stream([Message(role="user", content="one")], difficulty="low"))
        await _drain(gateway.stream([Message(role="user", content="two")], difficulty="low"))
    assert len(fake.credentials) == 1
    assert len(fake.requests) == 2


async def test_gateway_forwards_generation_params_to_provider():
    with fake_provider() as fake:
        gateway = _gateway()
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
    assert fake.last.options == {
        "reasoning_effort": "high",
        "temperature": 0.2,
        "top_p": 0.9,
        "stop": ["END"],
        "parallel_tool_calls": False,
    }


async def test_gateway_sends_the_system_prompt_and_tools_it_was_given():
    """What the harness built has to arrive on the wire unchanged — this is the only
    place that is checked without a live call."""
    tool = ToolDefinition(name="read", description="Read a file.", input_schema={"type": "object"})
    with fake_provider() as fake:
        gateway = _gateway()
        await _drain(
            gateway.stream(
                [Message(role="user", content="hi")],
                difficulty="low",
                system="You are stcode.",
                tools=[tool],
            )
        )
    assert fake.last.system == "You are stcode."
    assert fake.last.tool_names == ["read"]


async def test_gateway_retries_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)
    with fake_provider() as fake:
        fake.fail(RuntimeError("transient"), times=1)
        gateway = _gateway(
            retry={"max_attempts": 3, "base_delay": 0, "max_delay": 0, "jitter": False}
        )
        events = await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.requests) == 2
    assert isinstance(events[-1], MessageStop)


async def test_gateway_does_not_retry_non_transient_error(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: False)
    with fake_provider() as fake:
        fake.fail(ValueError("bad request"), times=1)
        gateway = _gateway()
        with pytest.raises(ValueError, match="bad request"):
            await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.requests) == 1


async def test_gateway_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)
    with fake_provider() as fake:
        fake.fail(RuntimeError("still down"), times=-1)
        gateway = _gateway(
            retry={"max_attempts": 2, "base_delay": 0, "max_delay": 0, "jitter": False}
        )
        with pytest.raises(RuntimeError, match="still down"):
            await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
    assert len(fake.requests) == 2


async def test_gateway_does_not_retry_after_partial_output(monkeypatch):
    """Once the caller holds a token, a retry would duplicate it."""
    monkeypatch.setattr(gateway_module, "_is_retryable", lambda exc: True)
    with fake_provider([[TextDelta(text="partial")]]) as fake:
        fake.raises_mid_stream = RuntimeError("dropped mid-stream")
        gateway = _gateway()
        received = []
        with pytest.raises(RuntimeError, match="dropped mid-stream"):
            async for event in gateway.stream(
                [Message(role="user", content="hi")], difficulty="low"
            ):
                received.append(event)
    assert len(received) == 1
    assert len(fake.requests) == 1


async def test_gateway_aclose_closes_every_cached_provider():
    with fake_provider() as fake:
        gateway = _gateway()
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
        await gateway.aclose()
    assert fake.closed


# ---- prompt caching ---------------------------------------------------------------


def _sent(monkeypatch, **stream_kwargs):
    """Capture the request body the Anthropic adapter builds, without a network call."""
    captured: dict = {}

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def __aiter__(self):
            async def empty():
                return
                yield  # pragma: no cover

            return empty()

    provider = AnthropicProvider(api_key="k")

    def fake_stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    monkeypatch.setattr(provider._client.messages, "stream", fake_stream)
    return provider, captured


async def test_cache_breakpoints_land_on_the_last_tool_system_and_message(monkeypatch):
    """Anthropic caches the request prefix up to each breakpoint, in the order
    tools -> system -> messages. All three, or the history — the bulk after ten tool
    calls — is re-billed in full every turn."""
    provider, captured = _sent(monkeypatch)
    tools = [
        ToolDefinition(name="a", description="A", input_schema={"type": "object"}),
        ToolDefinition(name="b", description="B", input_schema={"type": "object"}),
    ]
    async for _ in provider.stream(
        [Message(role="user", content="one"), Message(role="assistant", content="two")],
        model="claude-opus-5",
        system="be helpful",
        tools=tools,
    ):
        pass

    mark = {"type": "ephemeral"}
    assert "cache_control" not in captured["tools"][0]
    assert captured["tools"][-1]["cache_control"] == mark
    assert captured["system"][-1]["cache_control"] == mark

    # A string `content` is promoted to a block list — there is nowhere to hang
    # cache_control on a bare string.
    assert captured["messages"][0]["content"] == "one"
    assert captured["messages"][-1]["content"][-1]["cache_control"] == mark


async def test_no_tools_or_system_means_no_stray_breakpoints(monkeypatch):
    provider, captured = _sent(monkeypatch)
    async for _ in provider.stream([Message(role="user", content="hi")], model="m"):
        pass
    assert "tools" not in captured and "system" not in captured
    assert captured["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_usage_carries_the_cache_counters():
    """Without these, "turn 2 is ~10x cheaper" is an unverifiable claim."""
    usage = Usage(input_tokens=12, cache_read_input_tokens=9000)
    assert usage.cache_creation_input_tokens == 0
    assert usage.model_dump()["cache_read_input_tokens"] == 9000


async def test_the_openai_stream_is_closed_even_though_sse_never_reaches_eof(monkeypatch):
    """The SDK closes the response only if the body is read to completion, and an SSE
    body never is — iteration stops at `[DONE]`. Left to the GC it unwinds after the
    event loop has gone, surfacing as an httpcore traceback that names nothing of ours."""
    closed: list[bool] = []

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.append(True)
            return False

        def __aiter__(self):
            async def chunks():
                return
                yield  # pragma: no cover

            return chunks()

    provider = OpenAIProvider(api_key="k")

    async def fake_create(**_kwargs):
        return FakeStream()

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    events = [e async for e in provider.stream([Message(role="user", content="hi")], model="m")]

    assert closed == [True]
    assert isinstance(events[-1], MessageStop)


# ---- in-flight cap ---------------------------------------------------------------
#
# The failure this prevents: five parallel `task` calls become five simultaneous
# completions against one endpoint. A hosted API absorbs that; a local one serving a
# 27b model queues them and then drops the ones that waited too long, which arrives as
# a 503 that retrying cannot fix.


class _CountingProvider(BaseModelProvider):
    """Records how many streams are open at the same moment."""

    def __init__(self, api_key=None, base_url=None) -> None:
        super().__init__(api_key, base_url)
        self.live = 0
        self.peak = 0

    async def list_models(self):
        return ["m"]

    async def aclose(self) -> None:
        return None

    async def stream(self, messages, *, model, **_kwargs):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            # A real request suspends here. Without a suspension point the whole stream
            # runs between two scheduler ticks and nothing ever overlaps, which would
            # make this double report "capped" no matter what the gateway did.
            await asyncio.sleep(0.02)
            for event in says("ok"):
                yield event
        finally:
            self.live -= 1


@contextlib.contextmanager
def _counting_provider():
    instance = _CountingProvider()
    previous = PROVIDERS["openai"]
    PROVIDERS["openai"] = lambda api_key=None, base_url=None: instance  # type: ignore[assignment]
    try:
        yield instance
    finally:
        PROVIDERS["openai"] = previous


async def _five(gateway) -> None:
    """Five completions at once — one `task` fan-out, which is where this bites."""
    await asyncio.gather(
        *(
            _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
            for _ in range(5)
        )
    )


async def test_max_concurrent_serialises_requests_to_one_endpoint():
    with _counting_provider() as provider:
        gateway = _gateway(
            providers={"counting": {"provider": "openai", "api_key": "k", "max_concurrent": 1}},
            routing={"low": {"provider": "counting", "model": "m"}},
        )
        await _five(gateway)
    assert provider.peak == 1, f"{provider.peak} completions were open at once, not 1"


async def test_no_cap_means_no_cap():
    """0 is the default and has to stay free: a hosted API wants the parallelism."""
    with _counting_provider() as provider:
        gateway = _gateway(
            providers={"counting": {"provider": "openai", "api_key": "k"}},
            routing={"low": {"provider": "counting", "model": "m"}},
        )
        await _five(gateway)
    assert provider.peak > 1


async def test_an_abandoned_stream_releases_its_slot():
    """Every caller wraps iteration in `aclosing`, and a slot held by a stream nobody
    is reading is a deadlock rather than a slow turn."""
    with _counting_provider() as provider:
        gateway = _gateway(
            providers={"counting": {"provider": "openai", "api_key": "k", "max_concurrent": 1}},
            routing={"low": {"provider": "counting", "model": "m"}},
        )
        async with aclosing(
            gateway.stream([Message(role="user", content="hi")], difficulty="low")
        ) as stream:
            await stream.__anext__()
        # If the first one kept the slot this never returns.
        await asyncio.wait_for(
            _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low")), 2.0
        )


async def test_reconfigure_reaches_a_gateway_someone_else_is_holding():
    """`/model` has to change the key an already-running agent uses, and that agent
    holds this object rather than the daemon that built it."""
    with fake_provider() as fake:
        gateway = _gateway(providers={"fake": {"provider": "openai", "api_key": "old"}})
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))
        assert fake.last.api_key == "old"

        await gateway.reconfigure(
            _model_config(
                providers={"fake": {"provider": "openai", "api_key": "new"}},
                routing={"low": {"provider": "fake", "model": "m2"}},
            )
        )
        await _drain(gateway.stream([Message(role="user", content="hi")], difficulty="low"))

    assert fake.last.api_key == "new", "the cached client outlived the credentials"
    assert fake.last.model == "m2"
    assert fake.closed, "the client built around the old key was not closed"


def test_openai_parse_tool_arguments_repair_and_resilience():
    from stcode.core.providers.openai_gpt import _parse_tool_arguments

    assert _parse_tool_arguments("") == {}
    assert _parse_tool_arguments("   ") == {}
    assert _parse_tool_arguments('{"command": "pytest"}') == {"command": "pytest"}

    # Truncated unterminated strings (common when finish_reason='length')
    repaired1 = _parse_tool_arguments('{"command": "echo hello')
    assert repaired1 == {"command": "echo hello"}

    repaired2 = _parse_tool_arguments('{"command":"')
    assert repaired2 == {"command": ""}

    # Irreparable malformed input returns safe error dict rather than raising JSONDecodeError
    malformed = _parse_tool_arguments("{not json at all")
    assert malformed == {"_raw": "{not json at all", "_error": "JSONDecodeError"}

