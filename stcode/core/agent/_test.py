"""
Agent tests — the loop, against a scripted gateway and a real harness.

The gateway is the only fake. Faking the harness would mean testing our beliefs about
tool dispatch rather than the loop, and the loop's interesting properties are all about
what it writes to the session: that every `tool_call` gets a `tool_result` before the
next request (a dangling `tool_use` block is a 400 on the following turn), that usage
lands in the transcript and not in the model's history, and that an interrupt does not
leave the transcript in a state the next turn cannot send.

`ScriptedGateway` replays a list of turns. Each turn is a list of `StreamEvent`s, which
is exactly what a provider hands the gateway, so the loop is exercised through its real
interface rather than a convenience one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Coroutine, Iterator, Sequence, TypeVar

import pytest

from stcode.core.agent import Agent, AgentFailed, TextDelta, ToolFinished, ToolStarted, TurnFinished
from stcode.core.harness import Harness, HarnessContext
from stcode.core.providers.types import (
    Message,
    MessageStop,
    StreamEvent,
    ToolCallEnd,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from stcode.core.session import Session

T = TypeVar("T")


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def run(loop: asyncio.AbstractEventLoop) -> Any:
    def _run(coro: Coroutine[Any, Any, T]) -> T:
        return loop.run_until_complete(coro)

    return _run


class ScriptedGateway:
    """Replays one list of events per model call, and records what it was sent."""

    def __init__(self, turns: Sequence[Sequence[StreamEvent]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, **kwargs})
        if not self._turns:
            raise AssertionError("the agent asked for more turns than the script has")
        for event in self._turns.pop(0):
            yield event

    async def aclose(self) -> None:
        self.closed = True


class ExplodingGateway:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        raise self.exc
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        pass


def _text(*chunks: str) -> list[StreamEvent]:
    return [
        *(TextDelta(text=chunk) for chunk in chunks),
        MessageStop(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2)),
    ]


def _call(call_id: str, tool: str, **arguments: Any) -> list[StreamEvent]:
    return [
        ToolCallEnd(id=call_id, name=tool, input=arguments),
        MessageStop(stop_reason="tool_use", usage=Usage(input_tokens=20, output_tokens=5)),
    ]


def build(
    tmp_path: Path,
    turns: Sequence[Sequence[StreamEvent]] | Any,
    **kwargs: Any,
) -> Agent:
    gateway = turns if hasattr(turns, "stream") else ScriptedGateway(turns)
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    return Agent(
        gateway=gateway,  # type: ignore[arg-type]
        harness=Harness(HarnessContext(cwd=workspace), approval_mode="full-auto"),
        session=Session.create(cwd=workspace, directory=tmp_path / "sessions"),
        **kwargs,
    )


async def drain(agent: Agent, text: str) -> list[Any]:
    return [event async for event in agent.run(text)]


# ---- the stop condition ---------------------------------------------------------


def test_a_reply_with_no_tool_call_ends_the_turn(tmp_path: Path, run: Any) -> None:
    """The whole stop condition. No `answer` dict, no `ready` flag (EXPECTED.md §16)."""
    agent = build(tmp_path, [_text("Hello", " there")])
    events = run(drain(agent, "hi"))

    assert [type(e).__name__ for e in events] == ["TextDelta", "TextDelta", "TurnFinished"]
    assert events[-1].text == "Hello there"
    assert events[-1].usage.input_tokens == 10
    run(agent.aclose())


def test_the_loop_runs_a_tool_and_comes_back(tmp_path: Path, run: Any) -> None:
    (tmp_path / "work" / "f.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "f.txt").write_text("contents\n")

    agent = build(tmp_path, [_call("c1", "read", path="f.txt"), _text("It says contents.")])
    events = run(drain(agent, "what is in f.txt?"))

    assert [type(e).__name__ for e in events] == [
        "ToolStarted", "ToolFinished", "TextDelta", "TurnFinished",
    ]
    started, finished = events[0], events[1]
    assert isinstance(started, ToolStarted) and started.name == "read"
    assert isinstance(finished, ToolFinished) and finished.ok
    assert "contents" in finished.preview

    # The second request carries the first turn's call and its result.
    second = agent.gateway.calls[1]["messages"]  # type: ignore[attr-defined]
    assert isinstance(second[1].content[0], ToolUseBlock)
    assert isinstance(second[2].content[0], ToolResultBlock)
    run(agent.aclose())


def test_parallel_calls_all_run_and_all_get_results(tmp_path: Path, run: Any) -> None:
    turn = [
        ToolCallEnd(id="c1", name="ls", input={"path": "."}),
        ToolCallEnd(id="c2", name="ls", input={"path": "."}),
        MessageStop(stop_reason="tool_use", usage=Usage()),
    ]
    agent = build(tmp_path, [turn, _text("done")])
    run(drain(agent, "look around"))

    kinds = [(r["type"], r.get("id")) for r in agent.session.records()]
    assert ("tool_call", "c1") in kinds and ("tool_call", "c2") in kinds
    assert ("tool_result", "c1") in kinds and ("tool_result", "c2") in kinds
    run(agent.aclose())


def test_a_failing_tool_is_reported_to_the_model_not_raised(tmp_path: Path, run: Any) -> None:
    """`invoke()` never raises for a tool-level failure; the loop's only move is to hand
    the result back and let the model try something else."""
    agent = build(tmp_path, [_call("c1", "read", path="does-not-exist.txt"), _text("Missing.")])
    events = run(drain(agent, "read it"))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert not finished.ok
    assert isinstance(events[-1], TurnFinished)
    result = next(r for r in agent.session.records() if r["type"] == "tool_result")
    assert result["is_error"] is True
    run(agent.aclose())


# ---- what lands in the transcript -----------------------------------------------


def test_usage_is_recorded_but_never_sent_back(tmp_path: Path, run: Any) -> None:
    """Rule 7: the gateway emits usage, the agent writes it. And the model never sees
    it — paying to tell a model its own token count buys nothing."""
    agent = build(tmp_path, [_call("c1", "ls", path="."), _text("ok")])
    run(drain(agent, "hi"))

    usages = [r for r in agent.session.records() if r["type"] == "usage"]
    assert len(usages) == 2
    assert usages[0]["input_tokens"] == 20 and usages[0]["difficulty"] == agent.difficulty
    assert not any("input_tokens" in str(m.content) for m in agent.session.messages())
    run(agent.aclose())


def test_every_tool_call_has_a_result_before_the_next_request(tmp_path: Path, run: Any) -> None:
    """A `tool_use` block with no matching `tool_result` is a 400 on the next turn, and
    it looks perfectly fine in the JSONL — so assert on the pairing directly."""
    agent = build(tmp_path, [_call("c1", "ls", path="."), _call("c2", "ls", path="."), _text("ok")])
    run(drain(agent, "hi"))

    calls = {r["id"] for r in agent.session.records() if r["type"] == "tool_call"}
    results = {r["id"] for r in agent.session.records() if r["type"] == "tool_result"}
    assert calls == results == {"c1", "c2"}
    run(agent.aclose())


def test_a_provider_failure_ends_the_turn_and_is_logged(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, ExplodingGateway(RuntimeError("connection reset")))
    events = run(drain(agent, "hi"))

    assert isinstance(events[-1], AgentFailed)
    assert "connection reset" in events[-1].message
    assert any(r["type"] == "error" for r in agent.session.records())
    run(agent.aclose())


def test_the_tool_call_ceiling_fails_loudly(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, [_call(f"c{n}", "ls", path=".") for n in range(4)], max_turns=3)
    events = run(drain(agent, "loop forever"))

    assert isinstance(events[-1], AgentFailed)
    assert "ceiling of 3" in events[-1].message
    run(agent.aclose())


# ---- push / events / interrupt ---------------------------------------------------


def test_push_mid_turn_queues_for_the_next_turn(tmp_path: Path, run: Any) -> None:
    """§8.3: a push never splices into the turn in flight. This is what makes attaching
    to a working agent and redirecting it safe."""
    agent = build(tmp_path, [_text("first"), _text("second")])

    async def scenario() -> list[Any]:
        seen: list[Any] = []
        await agent.push("one")
        async for event in agent.events():
            if isinstance(event, TurnFinished):
                seen.append(event.text)
                if len(seen) == 1:
                    await agent.push("two")  # arrives while turn one is still finishing
                else:
                    return seen
        return seen

    assert run(scenario()) == ["first", "second"]
    user_messages = [r["content"] for r in agent.session.records() if r["type"] == "user"]
    assert user_messages == ["one", "two"]
    run(agent.aclose())


def test_interrupt_stops_the_turn_and_leaves_sendable_history(tmp_path: Path, run: Any) -> None:
    """The transcript after an interrupt must still be a valid conversation — an
    unanswered tool call would make the *next* turn fail, long after the Esc."""
    agent = build(tmp_path, [_call("c1", "ls", path="."), _text("unreached")])

    async def scenario() -> list[Any]:
        seen = []
        async for event in agent.run("go"):
            seen.append(event)
            if isinstance(event, ToolStarted):
                await agent.interrupt()
        return seen

    events = run(scenario())
    assert isinstance(events[-1], AgentFailed) and events[-1].message == "Interrupted."

    calls = {r["id"] for r in agent.session.records() if r["type"] == "tool_call"}
    results = {r["id"] for r in agent.session.records() if r["type"] == "tool_result"}
    assert calls == results
    run(agent.aclose())


def test_a_new_turn_clears_a_previous_interrupt(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, [_text("one"), _text("two")])
    run(agent.interrupt())
    events = run(drain(agent, "hi"))
    assert isinstance(events[-1], TurnFinished)
    assert not agent.harness.cancel.is_set()
    run(agent.aclose())


# ---- sub-agents ------------------------------------------------------------------


def test_task_spawns_a_child_with_its_own_session_and_narrower_tools(
    tmp_path: Path, run: Any
) -> None:
    parent = build(
        tmp_path,
        [
            _call(
                "c1",
                "task",
                prompt="Find the handlers.",
                name="scout",
                tools=["read", "grep"],
            ),
            _text("The scout found them."),
        ],
    )
    parent.enable_task()
    # The child streams from the same gateway, so its turn is scripted in between.
    parent.gateway._turns.insert(1, _text("Handlers are in src/api.py."))  # type: ignore[attr-defined]

    events = run(drain(parent, "where are the handlers?"))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.ok and "src/api.py" in finished.preview

    children = [s for s in Session.list(directory=tmp_path / "sessions") if s.get("parent")]
    assert len(children) == 1
    assert children[0]["agent_name"] == "scout"
    assert children[0]["parent"] == parent.session.id
    run(parent.aclose())


def test_task_is_advertised_only_when_enabled(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, [])
    assert "task" not in agent.harness.tool_names()
    agent.enable_task()
    assert "task" in agent.harness.tool_names()
    # Rule 3: what a sub-agent inherits must not include the ability to spawn.
    assert "task" not in agent.harness.for_subagent("child").tool_names()
    run(agent.aclose())


def test_a_sub_agent_refuses_to_spawn_deeper(tmp_path: Path, run: Any) -> None:
    parent = build(tmp_path, [])
    parent.enable_task()
    child_harness = parent.harness.for_subagent("child")
    child_harness.allow("task")  # even if something hands it the tool anyway

    result = run(child_harness.invoke("task", {"prompt": "go deeper", "name": "grandchild"}))
    assert result.is_error and "cannot spawn sub-agents" in result.content
    run(parent.aclose())


# ---- lifecycle -------------------------------------------------------------------


def test_a_shared_gateway_is_not_closed_by_the_agent(tmp_path: Path, run: Any) -> None:
    """A sub-agent closing the gateway would kill its parent's stream mid-turn."""
    agent = build(tmp_path, [])
    run(agent.aclose())
    assert agent.gateway.closed is False  # type: ignore[attr-defined]


def test_create_wires_config_through_to_harness_and_session(tmp_path: Path, run: Any) -> None:
    """The one test of `Agent.create`: everything else builds the parts directly, so
    this is where a mis-wired config section would otherwise go unnoticed."""
    from stcode.core.configs import GatewayConfig

    config = GatewayConfig.model_validate(
        {
            "defaults": {"provider": "anthropic", "model": "claude-opus-5", "approval_mode": "plan"},
            "providers": {"anthropic": {"api_key": "not-used"}},
            "routing": {"medium": {"provider": "anthropic", "model": "claude-sonnet-5"}},
            "agent": {"max_turns": 7, "difficulty": "low"},
            "session": {"dir": str(tmp_path / "sessions")},
        }
    )
    agent = run(
        Agent.create(config, cwd=tmp_path / "work", role="backend-dev", load_mcp=False)
    )

    assert agent.max_turns == 7 and agent.difficulty == "low"
    assert agent.harness.approval_mode == "plan"
    assert agent.harness.session_id == agent.session.id
    assert agent.session.path.parent == tmp_path / "sessions"
    assert agent.session.meta()["role"] == "backend-dev"
    # Plan mode forbids EXECUTE, so `task` is registered and correctly not advertised.
    assert "task" in agent.harness.registry and "task" not in agent.harness.tool_names()
    run(agent.aclose())


def test_cache_counters_reach_the_session(tmp_path: Path, run: Any) -> None:
    """Step 4's outcome has to be checkable after the fact, and the session is the one
    place it is recorded (rule 7 — no second logger)."""
    turn = [
        TextDelta(text="ok"),
        MessageStop(
            stop_reason="end_turn",
            usage=Usage(input_tokens=3, cache_read_input_tokens=9000, cache_creation_input_tokens=0),
        ),
    ]
    agent = build(tmp_path, [turn])
    run(drain(agent, "hi"))

    usage = next(r for r in agent.session.records() if r["type"] == "usage")
    assert usage["cache_read_input_tokens"] == 9000
    assert usage["input_tokens"] == 3
    run(agent.aclose())


def test_run_closes_the_generator_chain_when_it_returns_early(tmp_path: Path, run: Any) -> None:
    """`run()` returns on the first terminal event, abandoning events() -> _run_turn()
    -> _stream() -> the provider's stream. Left to the GC, anything holding a network
    connection down there unwinds after the loop has closed."""
    closed: list[str] = []

    class TrackingGateway(ScriptedGateway):
        async def stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
            try:
                async for event in super().stream(messages, **kwargs):
                    yield event
            finally:
                closed.append("stream")

    agent = build(tmp_path, TrackingGateway([_text("done")]))

    async def scenario() -> None:
        async for _ in agent.run("hi"):
            pass
        # No GC, no loop teardown: the chain must already be unwound right here.
        assert closed == ["stream"], "provider stream was left suspended"
        assert agent.busy is False

    run(scenario())
    run(agent.aclose())


# ---- step 9: the supervisor -----------------------------------------------------
#
# Two halves to test separately, because they fail differently. The heuristics are
# pure counting and must not fire on ordinary work. The nudge must reach the model as
# a `user` message and must leave the system prompt untouched, or prompt caching pays
# for every piece of advice.


def _calls(name: str, count: int, **arguments: Any) -> list[dict[str, Any]]:
    return [
        {"type": "tool_call", "id": str(n), "name": name, "arguments": arguments}
        for n in range(count)
    ]


def test_heuristics_stay_quiet_on_ordinary_work() -> None:
    """The property that matters most. A supervisor that fires on normal work is one
    you switch off, and then it catches nothing at all."""
    from stcode.core.agent.supervisor import Supervisor

    records = [
        {"type": "user", "content": "add a retry"},
        *_calls("grep", 1, pattern="retry"),
        {"type": "tool_result", "id": "0", "name": "grep", "content": "hit", "is_error": False},
        *_calls("read", 1, path="a.py"),
        {"type": "tool_result", "id": "0", "name": "read", "content": "...", "is_error": False},
        *_calls("edit", 1, path="a.py"),
        {"type": "tool_result", "id": "0", "name": "edit", "content": "ok", "is_error": False},
        *_calls("bash", 1, command="pytest"),
        {"type": "tool_result", "id": "0", "name": "bash", "content": "1 failed", "is_error": True},
    ]
    assert Supervisor.smell(records) is None


def test_heuristics_catch_the_four_shapes_of_stuck() -> None:
    from stcode.core.agent.supervisor import Supervisor

    repeated = Supervisor.smell(_calls("grep", 3, pattern="middleware"))
    assert repeated and "identical arguments 3 times" in repeated

    failing = Supervisor.smell(
        [
            {"type": "tool_result", "id": str(n), "is_error": n < 3, "content": ""}
            for n in range(4)
        ]
    )
    assert failing and "3 of its last 4" in failing

    # Four *different* edits to one file — not repetition, which the first check would
    # have caught, but the same file being reworked over and over.
    rewritten = Supervisor.smell(
        [
            {"type": "tool_call", "id": str(n), "name": "edit",
             "arguments": {"path": "src/main.py", "old": f"a{n}", "new": f"b{n}"}}
            for n in range(4)
        ]
    )
    assert rewritten and "src/main.py" in rewritten

    # Ten different reads is not repetition, and nothing was written: the "all looking,
    # no doing" shape, which needs its own check.
    idle = Supervisor.smell(
        [
            {"type": "tool_call", "id": str(n), "name": "read", "arguments": {"path": f"{n}.py"}}
            for n in range(10)
        ]
    )
    assert idle and "without writing a single file" in idle


def test_a_false_alarm_costs_nothing_beyond_one_cheap_call(run: Any) -> None:
    """The cheap model has a veto, and NONE is the common answer."""
    from stcode.core.agent.supervisor import Supervisor

    supervisor = Supervisor(ScriptedGateway([_text("NONE")]))  # type: ignore[arg-type]
    assert run(supervisor.check(_calls("grep", 3, pattern="x"))) is None


def test_the_same_nudge_is_not_repeated(run: Any) -> None:
    """Saying it twice is itself a loop, and an agent that ignored it once will ignore
    the repeat."""
    from stcode.core.agent.supervisor import Supervisor

    gateway = ScriptedGateway([_text("Stop grepping. Read mw.py."), _text("Stop grepping. Read mw.py.")])
    supervisor = Supervisor(gateway)  # type: ignore[arg-type]
    records = _calls("grep", 3, pattern="x")
    assert run(supervisor.check(records)) == "Stop grepping. Read mw.py."
    assert run(supervisor.check(records)) is None


def test_a_broken_supervisor_cannot_break_the_turn(run: Any) -> None:
    from stcode.core.agent.supervisor import Supervisor

    supervisor = Supervisor(ExplodingGateway(RuntimeError("down")))  # type: ignore[arg-type]
    assert run(supervisor.check(_calls("grep", 3, pattern="x"))) is None


def test_a_looping_agent_is_caught_and_redirected(tmp_path: Path, run: Any) -> None:
    """Phase 4's gate, against a scripted model: an agent that will not stop grepping.

    Eight identical calls, then the checkpoint. The nudge must arrive as a `user`
    message in the *next* request, and the system prompt must be byte-identical to the
    one before it.
    """
    from stcode.core.agent.supervisor import Supervisor

    looping = [_call(f"c{n}", "grep", pattern="middleware") for n in range(8)]
    gateway = ScriptedGateway([*looping, _text("Fine, I will read the file.")])
    agent = build(tmp_path, gateway, supervisor=Supervisor(ScriptedGateway([_text("You have grepped 'middleware' 8 times. Read mw.py instead.")])))  # type: ignore[arg-type]

    events = run(drain(agent, "find the middleware"))
    nudges = [e for e in events if type(e).__name__ == "SupervisorNudge"]
    assert len(nudges) == 1 and "Read mw.py" in nudges[0].text

    # It reached the model, as a user message rather than a prompt edit.
    last_request = gateway.calls[-1]
    rendered = [
        block.text
        for message in last_request["messages"]
        if message.role == "user"
        for block in (message.content if isinstance(message.content, list) else [])
        if getattr(block, "text", None)
    ] + [
        message.content
        for message in last_request["messages"]
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert any("[supervisor]" in text and "Read mw.py" in text for text in rendered)

    # And the cached prefix did not move.
    assert last_request["system"] == gateway.calls[0]["system"]
    assert [r["type"] for r in agent.session.records()].count("supervisor") == 1


def test_sub_agents_get_no_supervisor(tmp_path: Path, run: Any) -> None:
    """A nudge for a one-shot worker arrives about when the worker is finishing."""
    from stcode.core.configs import GatewayConfig

    config = GatewayConfig.model_validate(
        {
            "providers": {"anthropic": {"api_key": "not-used"}},
            "routing": {"low": {"provider": "anthropic", "model": "claude-haiku-4-5"}},
            "session": {"dir": str(tmp_path / "sessions")},
        }
    )
    parent = run(Agent.create(config, cwd=tmp_path / "work", load_mcp=False, load_repl=False))
    try:
        assert parent.supervisor is not None
        child_harness = parent.harness.for_subagent("worker")
        assert child_harness.depth == 1
        child = run(
            Agent.create(
                config,
                cwd=tmp_path / "work",
                load_mcp=False,
                load_repl=False,
                context=child_harness.context,
                depth=1,
            )
        )
        try:
            assert child.supervisor is None
        finally:
            run(child.aclose())
    finally:
        run(parent.aclose())
