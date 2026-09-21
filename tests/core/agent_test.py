"""
Agent tests — the loop, against a recording gateway and a real harness.

The gateway is the only fake. Faking the harness would mean testing our beliefs about
tool dispatch rather than the loop, and the loop's interesting properties are all about
what it writes to the session: that every `tool_call` gets a `tool_result` before the
next request (a dangling `tool_use` block is a 400 on the following turn), that usage
lands in the transcript and not in the model's history, and that an interrupt does not
leave the transcript in a state the next turn cannot send.

`RecordingGateway` (`tests/fakes.py`) replays a list of turns. Each turn is a list of
`StreamEvent`s, which is exactly what a provider hands the gateway, so the loop is
exercised through its real interface rather than a convenience one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest
from fakes import RecordingGateway, calls_tool, says

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


def build(
    tmp_path: Path,
    turns: Sequence[Sequence[StreamEvent]] | Any,
    **kwargs: Any,
) -> Agent:
    gateway = turns if hasattr(turns, "stream") else RecordingGateway(turns)
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
    """The whole stop condition. No `answer` dict, no `ready` flag."""
    agent = build(tmp_path, [says("Hello", " there")])
    events = run(drain(agent, "hi"))

    assert [type(e).__name__ for e in events] == ["TextDelta", "TextDelta", "TurnFinished"]
    assert events[-1].text == "Hello there"
    assert events[-1].usage.input_tokens == 10
    run(agent.aclose())


def test_the_loop_runs_a_tool_and_comes_back(tmp_path: Path, run: Any) -> None:
    (tmp_path / "work" / "f.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "f.txt").write_text("contents\n")

    agent = build(tmp_path, [calls_tool("c1", "read", path="f.txt"), says("It says contents.")])
    events = run(drain(agent, "what is in f.txt?"))

    assert [type(e).__name__ for e in events] == [
        "ToolStarted", "ToolFinished", "TextDelta", "TurnFinished",
    ]
    started, finished = events[0], events[1]
    assert isinstance(started, ToolStarted) and started.name == "read"
    assert isinstance(finished, ToolFinished) and finished.ok
    assert "contents" in finished.preview

    # The second request carries the first turn's call and its result.
    second = agent.gateway.calls[1].messages  # type: ignore[attr-defined]
    assert isinstance(second[1].content[0], ToolUseBlock)
    assert isinstance(second[2].content[0], ToolResultBlock)
    run(agent.aclose())


def test_parallel_calls_all_run_and_all_get_results(tmp_path: Path, run: Any) -> None:
    turn = [
        ToolCallEnd(id="c1", name="ls", input={"path": "."}),
        ToolCallEnd(id="c2", name="ls", input={"path": "."}),
        MessageStop(stop_reason="tool_use", usage=Usage()),
    ]
    agent = build(tmp_path, [turn, says("done")])
    run(drain(agent, "look around"))

    kinds = [(r["type"], r.get("id")) for r in agent.session.records()]
    assert ("tool_call", "c1") in kinds and ("tool_call", "c2") in kinds
    assert ("tool_result", "c1") in kinds and ("tool_result", "c2") in kinds
    run(agent.aclose())


def test_a_failing_tool_is_reported_to_the_model_not_raised(tmp_path: Path, run: Any) -> None:
    """`invoke()` never raises for a tool-level failure; the loop's only move is to hand
    the result back and let the model try something else."""
    agent = build(tmp_path, [calls_tool("c1", "read", path="does-not-exist.txt"), says("Missing.")])
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
    agent = build(tmp_path, [calls_tool("c1", "ls", path="."), says("ok")])
    run(drain(agent, "hi"))

    usages = [r for r in agent.session.records() if r["type"] == "usage"]
    assert len(usages) == 2
    assert usages[0]["input_tokens"] == 20 and usages[0]["difficulty"] == agent.difficulty
    assert not any("input_tokens" in str(m.content) for m in agent.session.messages())
    run(agent.aclose())


def test_every_tool_call_has_a_result_before_the_next_request(tmp_path: Path, run: Any) -> None:
    """A `tool_use` block with no matching `tool_result` is a 400 on the next turn, and
    it looks perfectly fine in the JSONL — so assert on the pairing directly."""
    agent = build(tmp_path, [calls_tool("c1", "ls", path="."), calls_tool("c2", "ls", path="."), says("ok")])
    run(drain(agent, "hi"))

    calls = {r["id"] for r in agent.session.records() if r["type"] == "tool_call"}
    results = {r["id"] for r in agent.session.records() if r["type"] == "tool_result"}
    assert calls == results == {"c1", "c2"}
    run(agent.aclose())


def test_a_provider_failure_ends_the_turn_and_is_logged(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, RecordingGateway(raises=RuntimeError("connection reset")))
    events = run(drain(agent, "hi"))

    assert isinstance(events[-1], AgentFailed)
    assert "connection reset" in events[-1].message
    assert any(r["type"] == "error" for r in agent.session.records())
    run(agent.aclose())


def test_the_tool_call_ceiling_fails_loudly(tmp_path: Path, run: Any) -> None:
    agent = build(tmp_path, [calls_tool(f"c{n}", "ls", path=".") for n in range(4)], max_turns=3)
    events = run(drain(agent, "loop forever"))

    assert isinstance(events[-1], AgentFailed)
    assert "ceiling of 3" in events[-1].message
    run(agent.aclose())


# ---- push / events / interrupt ---------------------------------------------------


def test_push_mid_turn_queues_for_the_next_turn(tmp_path: Path, run: Any) -> None:
    """§8.3: a push never splices into the turn in flight. This is what makes attaching
    to a working agent and redirecting it safe."""
    agent = build(tmp_path, [says("first"), says("second")])

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
    agent = build(tmp_path, [calls_tool("c1", "ls", path="."), says("unreached")])

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
    agent = build(tmp_path, [says("one"), says("two")])
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
            calls_tool(
                "c1",
                "task",
                prompt="Find the handlers.",
                name="scout",
                tools=["read", "grep"],
            ),
            says("The scout found them."),
        ],
    )
    parent.enable_task()
    # The child streams from the same gateway, so its turn is scripted in between.
    parent.gateway._turns.insert(1, says("Handlers are in src/api.py."))  # type: ignore[attr-defined]

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

    agents = tmp_path / "work" / ".stcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "backend-dev.md").write_text("# Role: backend dev\n\n## You own\nthe API.")

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

    class TrackingGateway(RecordingGateway):
        async def stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
            try:
                async for event in super().stream(messages, **kwargs):
                    yield event
            finally:
                closed.append("stream")

    agent = build(tmp_path, TrackingGateway([says("done")]))

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

    # Eight different reads is not repetition, and nothing was written: the "all looking,
    # no doing" shape, which needs its own check.
    idle = Supervisor.smell(
        [
            {"type": "tool_call", "id": str(n), "name": "read", "arguments": {"path": f"{n}.py"}}
            for n in range(8)
        ]
    )
    assert idle and "without writing a single file" in idle
    assert (
        Supervisor.smell(
            [
                {"type": "tool_call", "id": str(n), "name": "read", "arguments": {"path": f"{n}.py"}}
                for n in range(7)
            ]
        )
        is None
    )


def test_a_false_alarm_costs_nothing_beyond_one_cheap_call(run: Any) -> None:
    """The cheap model has a veto, and NONE is the common answer."""
    from stcode.core.agent.supervisor import Supervisor

    supervisor = Supervisor(RecordingGateway([says("NONE")]))  # type: ignore[arg-type]
    assert run(supervisor.check(_calls("grep", 3, pattern="x"))) is None


def test_the_same_nudge_is_not_repeated(run: Any) -> None:
    """Saying it twice is itself a loop, and an agent that ignored it once will ignore
    the repeat."""
    from stcode.core.agent.supervisor import Supervisor

    gateway = RecordingGateway([says("Stop grepping. Read mw.py."), says("Stop grepping. Read mw.py.")])
    supervisor = Supervisor(gateway)  # type: ignore[arg-type]
    records = _calls("grep", 3, pattern="x")
    assert run(supervisor.check(records)) == "Stop grepping. Read mw.py."
    assert run(supervisor.check(records)) is None


def test_a_broken_supervisor_cannot_break_the_turn(run: Any) -> None:
    from stcode.core.agent.supervisor import Supervisor

    supervisor = Supervisor(RecordingGateway(raises=RuntimeError("down")))  # type: ignore[arg-type]
    assert run(supervisor.check(_calls("grep", 3, pattern="x"))) is None


def test_a_looping_agent_is_caught_and_redirected(tmp_path: Path, run: Any) -> None:
    """Phase 4's gate, against a scripted model: an agent that will not stop grepping.

    Eight identical calls, then the checkpoint. The nudge must arrive as a `user`
    message in the *next* request, and the system prompt must be byte-identical to the
    one before it.
    """
    from stcode.core.agent.supervisor import Supervisor

    looping = [calls_tool(f"c{n}", "grep", pattern="middleware") for n in range(8)]
    gateway = RecordingGateway([*looping, says("Fine, I will read the file.")])
    agent = build(tmp_path, gateway, supervisor=Supervisor(RecordingGateway([says("You have grepped 'middleware' 8 times. Read mw.py instead.")])))  # type: ignore[arg-type]

    events = run(drain(agent, "find the middleware"))
    nudges = [e for e in events if type(e).__name__ == "SupervisorNudge"]
    assert len(nudges) == 1 and "Read mw.py" in nudges[0].text

    # It reached the model, as a user message rather than a prompt edit.
    last_request = gateway.calls[-1]
    rendered = [
        block.text
        for message in last_request.messages
        if message.role == "user"
        for block in (message.content if isinstance(message.content, list) else [])
        if getattr(block, "text", None)
    ] + [
        message.content
        for message in last_request.messages
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert any("[supervisor]" in text and "Read mw.py" in text for text in rendered)

    # And the cached prefix did not move.
    assert last_request.system == gateway.calls[0].system
    assert [r["type"] for r in agent.session.records()].count("supervisor") == 1


def test_sub_agents_get_no_supervisor(tmp_path: Path, run: Any) -> None:
    """A nudge for a one-shot worker arrives about when the worker is finishing."""
    from stcode.core.configs import GatewayConfig

    agents = tmp_path / "work" / ".stcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "backend-dev.md").write_text("# Role: backend dev\n\n## You own\nthe API.")

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


# ---- steering ------------------------------------------------------------------


def test_a_push_mid_turn_lands_at_the_next_tool_boundary(tmp_path: Path, run: Any) -> None:
    """Attaching to a working agent and redirecting it is the point of the daemon.

    Not into the model call in flight — at the boundary after this round of results,
    where every `tool_use` already has its `tool_result` and a `user` message is legal.
    """
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "f.txt").write_text("contents\n")
    agent = build(tmp_path, [calls_tool("c1", "read", path="f.txt"), says("done")])

    async def scenario() -> list[Any]:
        events = []
        async for event in agent.run("read the file"):
            events.append(event)
            if isinstance(event, ToolStarted):
                await agent.push("actually, stop after this one")
        return events

    run(scenario())

    kinds = [(r["type"], r.get("content")) for r in agent.session.records()]
    assert ("user", "actually, stop after this one") in kinds
    # And the model saw it: it arrived before the second request went out.
    second = agent.gateway.calls[1].messages  # type: ignore[attr-defined]
    assert "actually, stop after this one" in str(second[-1].content)
    run(agent.aclose())


def test_attach_wires_callbacks_and_a_custom_tool(tmp_path: Path, run: Any) -> None:
    """The one method a host needs: three callbacks and whatever tools it brings."""
    from stcode.core.harness.tools.base import tool

    @tool
    def weather(city: str) -> str:
        """Report the weather in one city."""
        return f"sunny in {city}"

    seen: list[str] = []

    def note(text: str) -> None:
        seen.append(text)

    agent = build(tmp_path, [])
    agent.attach(on_progress=note, tools=[weather])

    assert "weather" in agent.harness.tool_names()
    assert agent.harness.on_progress is note
    run(agent.harness.runtime().progress("working"))
    assert seen == ["working"]
    result = run(agent.harness.invoke("weather", {"city": "Hanoi"}))
    assert result.content == "sunny in Hanoi"
    run(agent.aclose())


def test_config_can_withhold_a_tool(tmp_path: Path) -> None:
    """`[agent] exclude_tools` is the operator's word, so it outranks every switch that
    added a tool — `task` included."""
    from stcode.core.agent.agent import apply_tool_policy
    from stcode.core.configs import AgentConfig

    agent = build(tmp_path, [])
    agent.enable_task()
    apply_tool_policy(agent.harness, AgentConfig(exclude_tools=["task", "web_search"]))

    names = agent.harness.tool_names()
    assert "task" not in names and "web_search" not in names and "read" in names


def test_config_naming_a_tool_that_does_not_exist_refuses(tmp_path: Path) -> None:
    """Quietly ignoring it would be diagnosed as 'the model ignores its tools', later."""
    from stcode.core.agent.agent import apply_tool_policy
    from stcode.core.configs import AgentConfig

    agent = build(tmp_path, [])
    with pytest.raises(ValueError, match="do not exist"):
        apply_tool_policy(agent.harness, AgentConfig(exclude_tools=["websearch"]))


# ---- the model is decided per call ------------------------------------------------


def test_a_model_override_reaches_the_gateway_on_the_next_call(
    tmp_path: Path, run: Any
) -> None:
    """`/model` mid-conversation takes effect on the next model call, not the next
    session. Read fresh per call, like the tool definitions, because a value cached at
    construction can no longer be changed by the person watching the turn."""
    agent = build(tmp_path, [says("one"), says("two")])
    run(drain(agent, "first"))
    assert agent.gateway.last.options.get("model") is None  # type: ignore[attr-defined]

    agent.session.set_meta(model="claude-opus-5", reasoning_effort="high")
    run(drain(agent, "second"))

    call = agent.gateway.last  # type: ignore[attr-defined]
    assert call.options["model"] == "claude-opus-5"
    assert call.options["reasoning_effort"] == "high"
    # The tier is a statement about this piece of work and is not what was overridden.
    assert call.difficulty == "high"
    run(agent.aclose())


def test_the_model_a_session_started_with_is_not_an_override(tmp_path: Path, run: Any) -> None:
    """`[defaults] model` is recorded in `meta` for the human reading the trajectory.
    Treating it as an override would pin every difficulty tier to one model."""
    agent = build(tmp_path, [says("hi")])
    agent.session.append(type="meta", **{})  # no-op record, not a settings change
    run(drain(agent, "go"))
    assert agent.gateway.last.options.get("model") is None  # type: ignore[attr-defined]
    run(agent.aclose())


# ---- a sub-agent's events go out the side ----------------------------------------


def test_a_subagents_events_are_forwarded_to_the_host(tmp_path: Path, run: Any) -> None:
    """The UI shows a sub-agent working. The parent's transcript does not grow because
    of it, and its tool output still never enters the parent's context."""
    seen: list[tuple[str, str]] = []
    parent = build(
        tmp_path,
        [
            calls_tool("c1", "task", prompt="Find the handlers.", name="scout"),
            says("The scout found them."),
        ],
    )
    parent.enable_task()
    parent.harness.on_event = lambda name, event: seen.append((name, event.type))
    parent.gateway._turns.insert(  # type: ignore[attr-defined]
        1, calls_tool("k1", "glob", pattern="*.py")
    )
    parent.gateway._turns.insert(2, says("Handlers are in src/api.py."))  # type: ignore[attr-defined]

    events = run(drain(parent, "where are the handlers?"))

    assert ("scout", "tool_started") in seen
    assert ("scout", "text_delta") in seen
    assert ("scout", "turn_finished") in seen
    # The parent's own events do not travel this way — they are its turn.
    assert [name for name, _ in seen] == ["scout"] * len(seen)
    # Nothing of the child's landed in the parent's transcript, and the parent got the
    # final message and nothing else.
    assert not any(r.get("name") == "glob" for r in parent.session.records())
    finished = next(e for e in events if isinstance(e, ToolFinished) and e.name == "task")
    assert "src/api.py" in finished.preview
    run(parent.aclose())


def test_an_unwatched_agent_forwards_nothing_and_does_not_care(
    tmp_path: Path, run: Any
) -> None:
    """`on_event` unset is the whole behaviour of an agent nobody is watching."""
    parent = build(
        tmp_path,
        [calls_tool("c1", "task", prompt="Look.", name="scout"), says("done")],
    )
    parent.enable_task()
    parent.gateway._turns.insert(1, says("nothing here"))  # type: ignore[attr-defined]
    assert parent.harness.on_event is None
    events = run(drain(parent, "look"))
    assert isinstance(events[-1], TurnFinished)
    run(parent.aclose())


# ---- a sub-agent that never finished ---------------------------------------------


def test_a_failed_sub_agent_is_an_error_not_a_paragraph(tmp_path: Path, run: Any) -> None:
    """The failure: five scouts never reach the model, each returns a sentence that
    begins "the sub-agent did not finish", and the parent — handed five *successful*
    tool results — reads them as findings and writes a confident report about five
    repositories nobody looked at.

    The child's turn is scripted to raise, which is what an overloaded endpoint
    dropping a queued request looks like from in here.
    """
    parent = build(
        tmp_path,
        [calls_tool("c1", "task", prompt="Investigate.", name="scout"), says("noted")],
    )
    parent.enable_task()

    gateway = parent.gateway
    original = gateway.stream

    def stream(messages: Any, **options: Any) -> Any:
        # The second call is the child's — the parent has already asked for the tool.
        if len(gateway.calls) >= 1:  # type: ignore[attr-defined]
            async def fails() -> Any:
                raise RuntimeError("503 Service Unavailable")
                yield  # pragma: no cover
            return fails()
        return original(messages, **options)

    gateway.stream = stream  # type: ignore[assignment,method-assign]

    events = run(drain(parent, "investigate the repos"))
    finished = next(e for e in events if isinstance(e, ToolFinished) and e.name == "task")
    assert not finished.ok, "a sub-agent that never reached the model reported success"
    assert "did not finish" in finished.preview
    run(parent.aclose())
