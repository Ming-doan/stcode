"""
Team tests — real files on a real volume.

The mailbox is a filesystem convention, so mocking it would test the mock. What is
worth catching here is the awkward half: that a reader never sees a half-written
message, that draining twice does not deliver twice, that a hand-off can only go to a
teammate that has joined, and that the `send_team_message` docstring's rule about `refs`
is actually enforced rather than merely requested.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from stcode.core.configs import load_config, resolve_prompt
from stcode.core.harness import Harness, HarnessContext
from stcode.core.team import Mailbox, make_find_teammate_tool, make_send_team_message_tool

DESCRIPTIONS = {
    "ba": "Writes the spec into /team/knowledge/spec.md.",
    "backend-dev": "Owns the API service and api-contract.md.",
    "frontend-dev": "Owns the web UI.",
}


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    """A `/team` with three roles that have joined it."""
    root = tmp_path / "team"
    for role, description in DESCRIPTIONS.items():
        Mailbox(root, role).join(description=description, team="shop")
    return root


def _team_harness(volume: Path, role: str = "ba") -> Harness:
    """A bare harness holding only the two team tools, as `role`."""
    mailbox = Mailbox(volume, role)
    harness = Harness(HarnessContext(cwd=volume), approval_mode="full-auto", tools=[])
    harness.registry.register(make_find_teammate_tool(mailbox))
    harness.registry.register(make_send_team_message_tool(mailbox))
    harness.allowed = ["find_teammate", "send_team_message"]
    return harness


# ---- the mailbox ---------------------------------------------------------------


def test_a_message_is_a_file_in_the_recipients_inbox(volume: Path) -> None:
    ba = Mailbox(volume, "ba")
    path = ba.send("backend-dev", "spec v2", "rate limiting", ["/team/knowledge/spec.md"])

    assert path.parent == volume / "inbox" / "backend-dev"
    written = json.loads(path.read_text())
    assert written["sender"] == "ba" and written["subject"] == "spec v2"
    assert written["refs"] == ["/team/knowledge/spec.md"]


def test_draining_delivers_once(volume: Path) -> None:
    """Twice would mean an agent re-reading the same instruction every turn forever."""
    Mailbox(volume, "ba").send("backend-dev", "one")
    backend = Mailbox(volume, "backend-dev")

    assert [m.subject for m in backend.drain()] == ["one"]
    assert backend.drain() == []
    # Moved, not deleted: "who told it that?" is a question you will ask.
    assert [entry["subject"] for entry in backend.history()] == ["one"]


def test_a_half_written_message_is_never_visible(volume: Path) -> None:
    """Delivery is a write then a rename, so a reader draining at the same moment sees
    a whole message or nothing."""
    inbox = volume / "inbox" / "backend-dev"
    (inbox / ".01HX-from-ba.json.partial").write_text('{"subject": "hal')

    assert Mailbox(volume, "backend-dev").pending() == []
    assert Mailbox(volume, "backend-dev").drain() == []


def test_inboxes_sort_by_time(volume: Path) -> None:
    ba = Mailbox(volume, "ba")
    for n in range(5):
        ba.send("backend-dev", f"message {n}")
    subjects = [m.subject for m in Mailbox(volume, "backend-dev").drain()]
    assert subjects == [f"message {n}" for n in range(5)]


def test_a_message_that_cannot_be_parsed_is_moved_aside(volume: Path) -> None:
    """Left in place it would be re-read every turn forever — a louder failure than
    losing one malformed file."""
    (volume / "inbox" / "backend-dev" / "01HX-from-ba.json").write_text("not json")
    backend = Mailbox(volume, "backend-dev")

    assert backend.drain() == []
    assert backend.pending() == []


# ---- joining ------------------------------------------------------------------


def test_joining_writes_a_member_card_and_an_inbox(volume: Path) -> None:
    card = json.loads((volume / "members" / "backend-dev.json").read_text())

    assert card["role"] == "backend-dev" and card["team"] == "shop"
    assert card["description"] == DESCRIPTIONS["backend-dev"]
    assert card["host"] and card["joined"]
    assert (volume / "inbox" / "backend-dev").is_dir()


def test_rejoining_replaces_the_card_rather_than_adding_one(volume: Path) -> None:
    """A restarted container is the same teammate, not a second one."""
    Mailbox(volume, "backend-dev").join(description="Owns the API, v2.")

    cards = [m for m in Mailbox(volume, "ba").members() if m.role == "backend-dev"]
    assert [card.description for card in cards] == ["Owns the API, v2."]


def test_a_card_that_cannot_be_parsed_is_skipped(volume: Path) -> None:
    """One broken file on a shared volume must not hide the whole team."""
    (volume / "members" / "qa.json").write_text("{not json")

    assert {m.role for m in Mailbox(volume, "ba").members()} == set(DESCRIPTIONS)


# ---- find_teammate -------------------------------------------------------------


def test_find_teammate_lists_everyone_but_the_caller(volume: Path, run: Any) -> None:
    result = run(_team_harness(volume).invoke("find_teammate", {}))

    assert not result.is_error
    assert "backend-dev" in result.content and "frontend-dev" in result.content
    assert DESCRIPTIONS["backend-dev"] in result.content
    assert "ba —" not in result.content


def test_find_teammate_filters_by_what_a_teammate_does(volume: Path, run: Any) -> None:
    """The caller knows the work it is handing off, not who owns it."""
    result = run(_team_harness(volume).invoke("find_teammate", {"query": "api"}))

    assert "backend-dev" in result.content
    assert "frontend-dev" not in result.content


def test_find_teammate_sees_a_teammate_that_joined_after_it_started(
    volume: Path, run: Any
) -> None:
    """The failure a list frozen into the system prompt has: a later container is
    invisible for the whole life of this one."""
    harness = _team_harness(volume)
    Mailbox(volume, "devops").join(description="Merges branches and deploys.")

    assert "devops" in run(harness.invoke("find_teammate", {"query": "deploy"})).content


def test_find_teammate_with_no_match_says_who_there_is(volume: Path, run: Any) -> None:
    result = run(_team_harness(volume).invoke("find_teammate", {"query": "kubernetes"}))

    assert "No teammate matches" in result.content
    assert "backend-dev" in result.content


# ---- send_team_message ---------------------------------------------------------


def test_send_team_message_refuses_a_role_that_has_not_joined(volume: Path, run: Any) -> None:
    """A message to a role with no card would sit in an inbox nobody reads."""
    result = run(_team_harness(volume).invoke("send_team_message", {"to": "qa", "subject": "hi"}))

    assert result.is_error and "no 'qa' on this team" in result.content
    assert "backend-dev" in result.content  # says who there is instead
    assert "find_teammate" in result.content
    assert not (volume / "inbox" / "qa").exists()


def test_send_team_message_refuses_a_body_that_should_have_been_a_file(
    volume: Path, run: Any
) -> None:
    """The rule that keeps a team's token cost from growing with the square of its
    size. A docstring asking nicely is not enough."""
    result = run(
        _team_harness(volume).invoke(
            "send_team_message",
            {"to": "backend-dev", "subject": "the spec", "body": "x" * 3000},
        )
    )
    assert result.is_error and "refs" in result.content


def test_send_team_message_refuses_a_ref_that_does_not_exist(volume: Path, run: Any) -> None:
    """A message pointing at nothing costs the recipient a whole turn to discover."""
    harness = _team_harness(volume)
    result = run(
        harness.invoke(
            "send_team_message",
            {"to": "backend-dev", "subject": "spec", "refs": ["/team/knowledge/nope.md"]},
        )
    )
    assert result.is_error and "do not exist yet" in result.content

    (volume / "knowledge" / "spec.md").write_text("# spec")
    ok = run(
        harness.invoke(
            "send_team_message",
            {"to": "backend-dev", "subject": "spec", "refs": ["knowledge/spec.md"]},
        )
    )
    assert not ok.is_error
    assert [m.subject for m in Mailbox(volume, "backend-dev").drain()] == ["spec"]


# ---- roles are data ------------------------------------------------------------


EXAMPLE_AGENTS = Path(__file__).resolve().parents[2] / "examples" / "agents"
"""The agent profiles the repository ships as a starting point. Not importable package
data — that is the point of the test below."""


def test_every_example_profile_is_a_valid_agent() -> None:
    """A new agent must be a new file and nothing else — and each one loads as a config
    on its own, because mounting it as `/config/config.toml` is how it is deployed."""
    paths = sorted(EXAMPLE_AGENTS.glob("*.toml"))
    assert {"ba", "backend-dev", "frontend-dev", "devops"} <= {path.stem for path in paths}
    for path in paths:
        config = load_config(path)
        assert config.team.role == path.stem
        # What its teammates see from `find_teammate`; empty is a teammate nobody finds.
        assert config.team.description
        body = resolve_prompt(config)
        assert body.startswith("# Role:")
        # Each one has to answer the three questions, or it is decoration.
        assert "## You own" in body and "## You read" in body and "## You report to" in body


def test_the_role_reaches_the_system_prompt(tmp_path: Path, run: Any) -> None:
    config = load_config(EXAMPLE_AGENTS / "backend-dev.toml")
    harness = run(
        Harness.create(
            tmp_path,
            agent_prompt=resolve_prompt(config),
            load_mcp=False,
            load_repl=False,
            load_skills=False,
        )
    )
    prompt = harness.system_prompt()
    assert "## Your role on this team" in prompt
    assert "api-contract.md" in prompt
    # And a sub-agent works inside the same role — it does not become role-less.
    assert "api-contract.md" in harness.for_subagent("worker").system_prompt()


def test_team_mode_is_off_until_it_is_switched_on(tmp_path: Path, run: Any) -> None:
    """Naming a role must not go looking for an inbox that is not mounted.

    The failure this prevents: a profile copied to a laptop used to start polling
    `/team` and refuse to run because the volume was not there.
    """
    from stcode.core.agent import Agent
    from stcode.core.configs import Config

    config = Config()
    config.agent.prompt = "# Role: tester\n\nYou run the suite."
    config.team.role = "tester"
    config.team.shared_dir = tmp_path / "not-mounted"
    config.supervisor.enabled = False
    config.mcp.enabled = False

    agent = run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]
    try:
        assert agent.mailbox is None, "a role alone turned team mode on"
        assert "send_team_message" not in agent.harness.tool_names()
        assert "You run the suite." in agent.harness.system_prompt()
    finally:
        run(agent.aclose())

    # And with the switch thrown, it joins — the volume is created on the way in, and
    # its card is there before the first turn.
    config.team.enabled = True
    config.team.description = "Runs the suite."
    agent = run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]
    try:
        assert agent.mailbox is not None
        assert {"find_teammate", "send_team_message"} <= set(agent.harness.tool_names())
        card = json.loads((tmp_path / "not-mounted" / "members" / "tester.json").read_text())
        assert card["description"] == "Runs the suite."
    finally:
        run(agent.aclose())


def test_team_mode_with_no_role_refuses(tmp_path: Path, run: Any) -> None:
    """A mailbox with no owner would address every message to ""."""
    from stcode.core.agent import Agent
    from stcode.core.configs import Config

    config = Config()
    config.team.enabled = True
    config.supervisor.enabled = False
    config.mcp.enabled = False
    with pytest.raises(ValueError, match="has to be somebody"):
        run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]


def test_a_team_url_refuses_until_there_is_a_service(tmp_path: Path, run: Any) -> None:
    """`url` is reserved for a team service. Quietly falling back to `shared_dir` would
    leave an operator who set it on a volume they think they replaced."""
    from stcode.core.agent import Agent
    from stcode.core.configs import Config

    config = Config()
    config.team.enabled = True
    config.team.role = "tester"
    config.team.url = "http://teams:7720"
    config.team.shared_dir = tmp_path / "team"
    config.supervisor.enabled = False
    config.mcp.enabled = False
    with pytest.raises(ValueError, match="url"):
        run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]
    assert not (tmp_path / "team").exists()


# ---- the agent side ------------------------------------------------------------


class _Scripted:
    """Replays one list of stream events per model call."""

    def __init__(self, turns: list[list[Any]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self.calls: list[dict[str, Any]] = []

    async def stream(self, messages: list[Any], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        for event in self._turns.pop(0) if self._turns else []:
            yield event

    async def aclose(self) -> None:
        pass


def _reply(text: str) -> list[Any]:
    from stcode.core.providers.types import MessageStop, TextDelta, Usage

    return [TextDelta(text=text), MessageStop(stop_reason="end_turn", usage=Usage())]


def _team_agent(volume: Path, tmp_path: Path, role: str, turns: list[list[Any]]) -> Any:
    from stcode.core.agent import Agent
    from stcode.core.session import Session

    agent = Agent(
        gateway=_Scripted(turns),  # type: ignore[arg-type]
        harness=Harness(HarnessContext(cwd=tmp_path), approval_mode="full-auto"),
        session=Session.create(cwd=tmp_path, role=role, directory=tmp_path / "sessions"),
    )
    agent.enable_task()
    agent.enable_team(str(volume), role)
    return agent


def test_joining_a_team_keeps_task(volume: Path, tmp_path: Path) -> None:
    """A team splits the product into roles; `task` splits one role's work inside its
    own checkout. Different axes, so joining a team takes nothing away."""
    agent = _team_agent(volume, tmp_path, "backend-dev", [])
    agent.enable_task()
    assert {"find_teammate", "send_team_message", "task"} <= set(agent.harness.tool_names())
    # But a sub-agent still cannot message another role: WORKER_TOOLS withholds both.
    worker = agent.harness.for_subagent("worker").tool_names()
    assert "send_team_message" not in worker and "find_teammate" not in worker


def test_waiting_messages_are_delivered_at_the_top_of_the_turn(
    volume: Path, tmp_path: Path, run: Any
) -> None:
    (volume / "knowledge" / "spec.md").write_text("# spec")
    Mailbox(volume, "ba").send("backend-dev", "spec v2", "rate limiting", ["knowledge/spec.md"])

    agent = _team_agent(volume, tmp_path, "backend-dev", [_reply("on it")])
    try:
        run(_drain(agent, "carry on"))
    finally:
        run(agent.aclose())

    kinds = [record["type"] for record in agent.session.records()]
    assert kinds.count("inbox") == 1

    # It reaches the model as a user message, like the supervisor's nudges — the cached
    # system prefix never moves for either.
    rendered = [
        message.content
        for message in agent.session.messages()
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert any("[message from ba]" in text and "knowledge/spec.md" in text for text in rendered)


async def _drain(agent: Any, text: str) -> list[Any]:
    return [event async for event in agent.run(text)]


def test_the_daemon_wakes_an_idle_agent_when_a_message_lands(
    volume: Path, tmp_path: Path, run: Any
) -> None:
    """§9.2's "how a colleague gets your attention", as one test."""
    from stcode.core.daemon.runner import WAKE, SessionRunner

    agent = _team_agent(volume, tmp_path, "backend-dev", [_reply("read it")])

    async def exercise() -> None:
        # Built inside the loop: `start()` and `watch_inbox()` both create tasks.
        runner = SessionRunner(agent)
        runner.start()
        runner.watch_inbox(agent.mailbox, interval=0.05)
        try:
            Mailbox(volume, "ba").send("backend-dev", "wake up")
            for _ in range(60):  # the poll interval, with room for a slow machine
                await asyncio.sleep(0.05)
                if any(r["type"] == "inbox" for r in agent.session.records()):
                    return
        finally:
            await runner.aclose()

    run(exercise())

    kinds = [record["type"] for record in agent.session.records()]
    assert "inbox" in kinds, "the watcher never started a turn"
    assert any(
        record.get("content") == WAKE for record in agent.session.records()
        if record["type"] == "user"
    )
