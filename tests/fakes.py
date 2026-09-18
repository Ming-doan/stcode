"""
Test doubles — three of them, at three levels, and the level is the point.

Each one exists to audit *what the layer above it sent*, so pick the lowest double that
still leaves the code under test real:

| Double | Replaces | Leaves real | Use it to audit |
| --- | --- | --- | --- |
| `FakeProvider` | the vendor SDK | `LLMGateway` | routing, retry, credential resolution, the request on the wire |
| `RecordingGateway` | `LLMGateway` | `Agent`, `Harness` | the turn loop: messages, tools and system prompt per call |
| `FakeAgent` | `Agent` | `SessionRunner`, `Daemon` | the protocol: what the daemon frames and broadcasts |

None of them opens a network connection. A test that wants a real API is marked `live`
and is deselected by default — see `pytest.ini`.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Sequence

from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.registry import PROVIDERS
from stcode.core.providers.types import (
    Message,
    MessageStop,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    ToolDefinition,
    Usage,
)

# ---- scripts --------------------------------------------------------------------
#
# A "turn" is one list of `StreamEvent`s — exactly what a provider hands the gateway —
# so the code under test is exercised through its real interface rather than a
# convenience one. These two builders cover every turn an agent loop can take: one that
# talks, and one that calls a tool.


def says(*chunks: str, input_tokens: int = 10, output_tokens: int = 2) -> list[StreamEvent]:
    """A turn that replies with text and stops. The loop's terminal case."""
    return [
        *(TextDelta(text=chunk) for chunk in chunks),
        MessageStop(
            stop_reason="end_turn",
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
    ]


def calls_tool(call_id: str, tool: str, **arguments: Any) -> list[StreamEvent]:
    """A turn that calls one tool. The loop's continuing case."""
    return [
        ToolCallEnd(id=call_id, name=tool, input=arguments),
        MessageStop(stop_reason="tool_use", usage=Usage(input_tokens=20, output_tokens=5)),
    ]


# ---- the provider ---------------------------------------------------------------


@dataclass
class ProviderRequest:
    """One `stream()` as the provider received it — every argument, plus the
    credentials the gateway resolved before constructing us."""

    messages: list[Message]
    model: str
    system: str | None = None
    tools: list[ToolDefinition] | None = None
    max_tokens: int = 8192
    api_key: str | None = None
    base_url: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [entry.name for entry in self.tools or []]

    @property
    def texts(self) -> list[str]:
        """Each message's text, for asserting on history without unpacking blocks."""
        return [
            message.content
            if isinstance(message.content, str)
            else " ".join(
                getattr(block, "text", "") or getattr(block, "content", "")
                for block in message.content
            )
            for message in self.messages
        ]


class FakeProvider(BaseModelProvider):
    """A provider that records what it was asked and replays a script.

    The lowest useful double: with this in the registry the **real** `LLMGateway` runs,
    so a test can assert that a tier routed where it should, that a retry actually
    retried, and that the system prompt and tool definitions reaching the wire are the
    ones the harness built.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        script: Sequence[Sequence[StreamEvent]] = (),
        models: Sequence[str] = ("fake-small", "fake-large"),
    ) -> None:
        super().__init__(api_key, base_url)
        self.requests: list[ProviderRequest] = []
        self.credentials: list[tuple[str | None, str | None]] = []
        self.closed = False
        self._script = [list(turn) for turn in script]
        self._models = list(models)
        self.raises: Exception | None = None
        self.fail_times = 0
        """Set through `fail()`. The gateway's retry policy is the thing under test, so
        a double has to be able to fail a *bounded* number of times and then work."""

        self.raises_mid_stream: Exception | None = None
        """Raised after the turn's events have been yielded. The retry loop must not
        retry once the caller holds partial content — retrying would duplicate it."""

    async def list_models(self) -> list[str]:
        return list(self._models)

    async def aclose(self) -> None:
        self.closed = True

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        reasoning_effort: Any = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(
            ProviderRequest(
                messages=list(messages),
                model=model,
                system=system,
                tools=list(tools) if tools else None,
                max_tokens=max_tokens,
                api_key=self.api_key,
                base_url=self.base_url,
                options={
                    "reasoning_effort": reasoning_effort,
                    "temperature": temperature,
                    "top_p": top_p,
                    "stop": stop,
                    "parallel_tool_calls": parallel_tool_calls,
                },
            )
        )
        if self.raises is not None and self.fail_times != 0:
            self.fail_times -= 1
            raise self.raises
        # Default to a plain reply rather than raising: most tests care about the
        # request, not the response, and scripting one turn per call to get there is
        # noise in every one of them.
        turn = self._script.pop(0) if self._script else says("ok")
        for event in turn:
            yield event
        if self.raises_mid_stream is not None:
            raise self.raises_mid_stream

    def fail(self, exc: Exception, times: int = 1) -> None:
        """Make the next `times` calls raise `exc`. A negative count never recovers."""
        self.raises, self.fail_times = exc, times

    @property
    def last(self) -> ProviderRequest:
        assert self.requests, "the provider was never called"
        return self.requests[-1]


@contextmanager
def fake_provider(
    script: Sequence[Sequence[StreamEvent]] = (),
    *,
    name: str = "fake",
    models: Sequence[str] = ("fake-small", "fake-large"),
) -> Iterator[FakeProvider]:
    """Register `name` in the provider registry for the block, backed by one instance.

    `get_provider` calls `PROVIDERS[name](api_key=..., base_url=...)`, so what goes in
    the registry is a factory that hands back the same object every time and notes the
    credentials it was handed. That is what lets a test assert the gateway resolved a
    tier's key over the provider's, and an env var over a literal.
    """
    instance = FakeProvider(script=script, models=models)

    def factory(api_key: str | None = None, base_url: str | None = None) -> FakeProvider:
        instance.credentials.append((api_key, base_url))
        instance.api_key, instance.base_url = api_key, base_url
        return instance

    previous = PROVIDERS.get(name)
    PROVIDERS[name] = factory  # type: ignore[assignment]
    try:
        yield instance
    finally:
        if previous is None:
            PROVIDERS.pop(name, None)
        else:
            PROVIDERS[name] = previous


# ---- the gateway ----------------------------------------------------------------


@dataclass
class GatewayCall:
    """One `stream()` as the gateway received it from the agent loop."""

    messages: list[Message]
    system: str | None = None
    tools: list[ToolDefinition] | None = None
    difficulty: str = "high"
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [entry.name for entry in self.tools or []]


class RecordingGateway:
    """Replays one scripted turn per model call, recording what it was sent.

    Duck-typed rather than a `LLMGateway` subclass: `Agent` only ever calls `stream()`
    and `aclose()`, and inheriting would drag in the routing this double exists to skip.
    """

    def __init__(
        self,
        turns: Sequence[Sequence[StreamEvent]] = (),
        *,
        raises: Exception | None = None,
    ) -> None:
        self._turns = [list(turn) for turn in turns]
        self.calls: list[GatewayCall] = []
        self.closed = False
        self.raises = raises
        """Raised instead of streaming. A provider failure must end the turn with
        `AgentFailed` and still leave a transcript the next turn can send."""

        self.gate: asyncio.Event | None = None
        """Set to hold a turn open, so a test can detach or interrupt mid-run."""

    async def stream(
        self,
        messages: list[Message],
        *,
        difficulty: str = "high",
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
        **options: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(
            GatewayCall(
                messages=list(messages),
                system=system,
                tools=list(tools) if tools else None,
                difficulty=difficulty,
                options=options,
            )
        )
        if self.raises is not None:
            raise self.raises
        if self.gate is not None:
            await self.gate.wait()
        if not self._turns:
            raise AssertionError("the agent asked for more turns than the script has")
        for event in self._turns.pop(0):
            yield event

    async def aclose(self) -> None:
        self.closed = True

    @property
    def last(self) -> GatewayCall:
        assert self.calls, "the gateway was never called"
        return self.calls[-1]


# ---- the agent ------------------------------------------------------------------


class FakeHarness:
    """Just enough harness for `SessionRunner.describe()` and `set_mode`."""

    def __init__(self, approval_mode: str = "suggest") -> None:
        self.approval_mode = approval_mode
        self.agent_name = "main"


class FakeAgent:
    """An agent that emits what a test tells it to, and records what it was pushed.

    The daemon's job is framing and fan-out: turn an `AgentEvent` into a line, park a
    tool on a `Future` until a client answers, and keep doing both after the last
    client leaves. A real `Agent` can demonstrate that, but only by also exercising a
    model call, a harness and a tool — so a failure says "the daemon is broken" when
    the truth was "the tool changed".

    The `session` is real: it is a file and a list of dicts, and `History` replay is
    only meaningful against records that were actually appended.
    """

    def __init__(self, session: Any, *, approval_mode: str = "suggest") -> None:
        self.session = session
        self.harness = FakeHarness(approval_mode)
        self.mailbox: Any = None
        self.pushed: list[str] = []
        self.interrupted = 0
        self.closed = False
        self._busy = False
        self._outbox: asyncio.Queue[Any] = asyncio.Queue()
        self.on_approval: Any = None
        self.on_ask: Any = None
        self.on_progress: Any = None

    # ---- the surface `SessionRunner` uses ----

    def attach(self, *, on_ask: Any = None, on_approval: Any = None, on_progress: Any = None, tools: Any = ()) -> "FakeAgent":
        self.on_ask, self.on_approval, self.on_progress = on_ask, on_approval, on_progress
        return self

    @property
    def busy(self) -> bool:
        return self._busy

    async def push(self, text: str) -> None:
        self.pushed.append(text)
        self.session.append(type="user", content=text)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def events(self) -> AsyncIterator[Any]:
        while True:
            yield await self._outbox.get()

    async def aclose(self) -> None:
        self.closed = True
        self.session.close()

    # ---- what a test drives it with ----

    def emit(self, event: Any) -> None:
        """Put one `AgentEvent` on the stream the runner is draining."""
        self._outbox.put_nowait(event)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy


__all__ = [
    "FakeAgent",
    "FakeHarness",
    "FakeProvider",
    "GatewayCall",
    "ProviderRequest",
    "RecordingGateway",
    "calls_tool",
    "fake_provider",
    "says",
]
