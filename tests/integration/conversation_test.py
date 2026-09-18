"""
A long conversation, through the whole stack, with only the SDK replaced.

`tests/core/agent_test.py` checks the loop one property at a time against a handful of
turns. This checks what only shows up over length: that after twenty model calls and a
dozen tool results the transcript is still something a provider will accept, the cached
prefix has not moved, and a session reopened from disk is the conversation that
happened rather than an empty one appended to an old file.

Every tool here is real and every file it touches is real — the fixture workspace is a
small project precisely so that `grep`, `read` and `edit` have something to be right or
wrong about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fakes import calls_tool, fake_provider, says

from stcode.core.agent import Agent, TurnFinished
from stcode.core.configs import GatewayConfig, load_config
from stcode.core.providers.types import ToolResultBlock, ToolUseBlock
from stcode.core.session import Session

# One turn of work, as a model would actually spend it: look, read, change, verify, say.
INVESTIGATE = [
    calls_tool("c1", "grep", pattern="to_cents", path="src"),
    calls_tool("c2", "read", path="src/util.py"),
    calls_tool("c3", "edit", path="src/util.py", old_string="RETRY_LIMIT = 3", new_string="RETRY_LIMIT = 5"),
    calls_tool("c4", "ls", path="src"),
    says("Raised the retry limit to 5."),
]


def build(workspace: Path, config_path: Path, **overrides: Any) -> GatewayConfig:
    """The fixture project's config, with transcripts kept inside the copied workspace.

    The supervisor is off unless a test asks for it: it shares the agent's gateway, so
    leaving it on would have every test script account for calls it does not care about.
    """
    config = load_config(config_path)
    config.session.dir = workspace / ".stcode" / "sessions"
    config.supervisor.enabled = overrides.pop("supervisor", False)
    for key, value in overrides.items():
        setattr(config.agent, key, value)
    return config


async def _drain(agent: Agent, text: str) -> list[Any]:
    return [event async for event in agent.run(text)]


def test_twenty_model_calls_leave_a_transcript_a_provider_will_accept(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """The failure this exists to catch is a 400 on turn eleven.

    Every `tool_use` block a provider is sent must have a matching `tool_result` in the
    next user message. One missing pair anywhere in the history poisons every request
    after it, and nothing before that point looks wrong.
    """
    script = [*INVESTIGATE, *INVESTIGATE, *INVESTIGATE, *INVESTIGATE]
    with fake_provider(script) as fake:
        config = build(workspace, config_path)
        agent = run(Agent.create(config, cwd=workspace, load_mcp=False))
        try:
            for question in (
                "Raise the retry limit.",
                "Do it again, I reverted.",
                "And once more.",
                "Last time.",
            ):
                events = run(_drain(agent, question))
                assert isinstance(events[-1], TurnFinished)
        finally:
            run(agent.aclose())

    assert len(fake.requests) == 20

    for request in fake.requests:
        offered: list[str] = []
        answered: list[str] = []
        for message in request.messages:
            if isinstance(message.content, str):
                continue
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    offered.append(block.id)
                elif isinstance(block, ToolResultBlock):
                    answered.append(block.tool_use_id)
        # The last call of a turn is the only one allowed an unanswered block, and even
        # then only because the request *is* the question about it.
        assert answered == offered[: len(answered)]
        assert len(offered) - len(answered) <= 1


def test_the_cached_prefix_does_not_move_across_the_conversation(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """Prompt caching is the biggest cost lever there is, and it is bought entirely
    with byte-stability. A section that reorders, or a tool list that sorts differently
    between turns, turns every cache read into a cache write."""
    with fake_provider([*INVESTIGATE, *INVESTIGATE]) as fake:
        config = build(workspace, config_path)
        agent = run(Agent.create(config, cwd=workspace, role="backend-dev", load_mcp=False))
        try:
            run(_drain(agent, "Raise the retry limit."))
            run(_drain(agent, "Again please."))
        finally:
            run(agent.aclose())

    systems = {request.system for request in fake.requests}
    assert len(systems) == 1, "the system prompt changed mid-conversation"

    tool_lists = {tuple(request.tool_names) for request in fake.requests}
    assert len(tool_lists) == 1, "the tool list reordered between turns"
    # Sorted, because "same set" is not enough: two orderings of the same tools are two
    # different cache prefixes.
    names = list(tool_lists.pop())
    assert names == sorted(names)


def test_history_grows_by_exactly_what_happened(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """Each model call sees one more assistant turn and one more batch of results than
    the last. A history that grows faster is duplicating; slower is dropping."""
    with fake_provider(list(INVESTIGATE)) as fake:
        config = build(workspace, config_path)
        agent = run(Agent.create(config, cwd=workspace, load_mcp=False))
        try:
            run(_drain(agent, "Raise the retry limit."))
        finally:
            run(agent.aclose())

    lengths = [len(request.messages) for request in fake.requests]
    assert lengths == sorted(lengths)
    assert lengths[0] == 1  # just the user's question
    # One assistant message and one results message per completed tool round.
    assert lengths == [1, 3, 5, 7, 9]


def test_a_resumed_session_continues_the_conversation_it_reopened(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """`cp session.jsonl` is the branching feature, which only works if reopening a
    file means adopting it. An agent that appends to a transcript it never read would
    send the model a conversation neither of them had."""
    with fake_provider([*INVESTIGATE, *INVESTIGATE]) as fake:
        config = build(workspace, config_path)
        agent = run(Agent.create(config, cwd=workspace, load_mcp=False))
        session_id = agent.session.id
        try:
            run(_drain(agent, "Raise the retry limit."))
        finally:
            run(agent.aclose())

        reopened = Session.resume(session_id, directory=config.session.dir)
        resumed = run(
            Agent.create(config, cwd=workspace, session=reopened, load_mcp=False)
        )
        try:
            run(_drain(resumed, "Again please."))
        finally:
            run(resumed.aclose())

    first_request_after_resume = fake.requests[5]
    texts = first_request_after_resume.texts
    assert "Raise the retry limit." in texts[0]
    assert texts[-1] == "Again please."
    # The whole first turn came back, not just its last message.
    assert len(first_request_after_resume.messages) > 2


def test_the_supervisor_nudges_through_history_not_through_the_prompt(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """A nudge in the system prompt would move the cached prefix, so every piece of
    advice would cost a full cache miss. It goes in as a `user` message instead — and
    it is bought at the cheap tier."""
    def grep(n: int) -> list[Any]:
        return calls_tool(f"c{n}", "grep", pattern="to_cents", path="src")

    # The supervisor shares this provider, so the script has to account for its calls:
    # the agent checks in every fourth iteration, and that check is a model call too.
    script = [
        *(grep(n) for n in range(4)),
        says("You have grepped for to_cents four times. Read src/util.py instead."),
        *(grep(n) for n in range(4, 8)),
        says("Still grepping. Read the file."),
        says("I will read the file instead."),
    ]
    with fake_provider(script) as fake:
        config = build(workspace, config_path, supervisor=True)
        agent = run(Agent.create(config, cwd=workspace, load_mcp=False))
        try:
            events = run(_drain(agent, "find to_cents"))
        finally:
            run(agent.aclose())

    assert any(type(event).__name__ == "SupervisorNudge" for event in events)

    # The supervisor's own call went to the `low` tier, not the agent's `high` one.
    models = {request.model for request in fake.requests}
    assert "fake-small" in models and "fake-large" in models

    # And the prefix the expensive tier sees never moved.
    systems = {request.system for request in fake.requests if request.model == "fake-large"}
    assert len(systems) == 1

    nudged = [record for record in agent.session.records() if record["type"] == "supervisor"]
    assert nudged, "the nudge was never written to the transcript"
