"""
Team tests — real files on a real volume.

The mailbox is a filesystem convention, so mocking it would test the mock. What is
worth catching here is the awkward half: that a reader never sees a half-written
message, that draining twice does not deliver twice, and that the `send_message`
docstring's rule about `refs` is actually enforced rather than merely requested.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from stcode.core.harness import Harness, HarnessContext
from stcode.core.harness.prompts import available_agents, load_agent_prompt
from stcode.core.team import Mailbox, make_send_message_tool

@pytest.fixture
def volume(tmp_path: Path) -> Path:
    """A `/team` with two roles on it."""
    mailbox = Mailbox(tmp_path / "team", "ba")
    mailbox.ensure("backend-dev", "frontend-dev")
    return tmp_path / "team"


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


# ---- the tool ------------------------------------------------------------------


def test_send_message_refuses_a_role_that_is_not_on_the_team(volume: Path, run: Any) -> None:
    harness = Harness(HarnessContext(cwd=volume), approval_mode="full-auto", tools=[])
    harness.registry.register(make_send_message_tool(Mailbox(volume, "ba")))
    harness.allowed = ["send_message"]

    result = run(harness.invoke("send_message", {"to": "qa", "subject": "hi"}))
    assert result.is_error and "no 'qa' on this team" in result.content
    assert "backend-dev" in result.content  # says who there is instead


def test_send_message_refuses_a_body_that_should_have_been_a_file(
    volume: Path, run: Any
) -> None:
    """The rule that keeps a team's token cost from growing with the square of its
    size. A docstring asking nicely is not enough."""
    harness = Harness(HarnessContext(cwd=volume), approval_mode="full-auto", tools=[])
    harness.registry.register(make_send_message_tool(Mailbox(volume, "ba")))
    harness.allowed = ["send_message"]

    result = run(
        harness.invoke(
            "send_message",
            {"to": "backend-dev", "subject": "the spec", "body": "x" * 3000},
        )
    )
    assert result.is_error and "refs" in result.content


def test_send_message_refuses_a_ref_that_does_not_exist(volume: Path, run: Any) -> None:
    """A message pointing at nothing costs the recipient a whole turn to discover."""
    harness = Harness(HarnessContext(cwd=volume), approval_mode="full-auto", tools=[])
    harness.registry.register(make_send_message_tool(Mailbox(volume, "ba")))
    harness.allowed = ["send_message"]

    result = run(
        harness.invoke(
            "send_message",
            {"to": "backend-dev", "subject": "spec", "refs": ["/team/knowledge/nope.md"]},
        )
    )
    assert result.is_error and "do not exist yet" in result.content

    (volume / "knowledge" / "spec.md").write_text("# spec")
    ok = run(
        harness.invoke(
            "send_message", {"to": "backend-dev", "subject": "spec", "refs": ["knowledge/spec.md"]}
        )
    )
    assert not ok.is_error


# ---- roles are data ------------------------------------------------------------


EXAMPLE_AGENTS = Path(__file__).resolve().parents[2] / "examples" / "agents"
"""The agent profiles the repository ships as a starting point. Not importable package
data — that is the point of the test below."""


def _profile(directory: Path, name: str, prompt: str, **agent: object) -> Path:
    """Write a minimal agent profile. What a deployment mounts, in three lines."""
    directory.mkdir(parents=True, exist_ok=True)
    keys = "".join(f"{key} = {value!r}\n" for key, value in agent.items())
    path = directory / f"{name}.toml"
    path.write_text(f'[agent]\n{keys}prompt = """\n{prompt}\n"""\n', encoding="utf-8")
    return path


def test_every_example_profile_is_a_valid_agent(monkeypatch: Any) -> None:
    """A new agent must be a new file and nothing else.

    Pointed at through `STCODE_AGENTS_DIR`, which is also how a container reaches them:
    one directory and nothing else.
    """
    monkeypatch.setenv("STCODE_AGENTS_DIR", str(EXAMPLE_AGENTS))
    names = sorted(path.stem for path in EXAMPLE_AGENTS.glob("*.toml"))
    assert {"ba", "backend-dev", "frontend-dev", "devops"} <= set(names)
    assert available_agents() == names
    for name in names:
        body = load_agent_prompt(name)
        assert body.startswith("# Role:")
        # Each one has to answer the three questions, or it is decoration.
        assert "## You own" in body and "## You read" in body and "## You report to" in body


def test_a_profile_is_read_from_the_agents_directory(tmp_path: Path) -> None:
    """Project `.stcode/agents/` first, then the user's config directory."""
    _profile(tmp_path / ".stcode" / "agents", "tester", "# Role: tester\n\n## You own\nthe suite.")

    assert "tester" in available_agents(tmp_path)
    assert load_agent_prompt("tester", tmp_path).startswith("# Role: tester")


def test_a_profile_can_keep_its_prompt_in_a_file(tmp_path: Path) -> None:
    """`prompt_file` is relative to the profile, so a directory of agents moves whole."""
    agents = tmp_path / ".stcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "tester.md").write_text("# Role: tester\n\nYou run the suite.")
    (agents / "tester.toml").write_text('[agent]\nprompt_file = "tester.md"\n')

    assert load_agent_prompt("tester", tmp_path) == "# Role: tester\n\nYou run the suite."


def test_a_profile_naming_both_prompts_refuses(tmp_path: Path) -> None:
    """A precedence rule is one more thing to be wrong about at 3am."""
    agents = tmp_path / ".stcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "tester.toml").write_text(
        '[agent]\nprompt = "inline"\nprompt_file = "tester.md"\n'
    )
    with pytest.raises(ValueError, match="one prompt"):
        load_agent_prompt("tester", tmp_path)


def test_an_unknown_agent_refuses_loudly(tmp_path: Path) -> None:
    """A mount that did not happen must not degrade into a role-less agent."""
    with pytest.raises(FileNotFoundError, match="No agent named"):
        load_agent_prompt("backedn-dev", tmp_path)


def test_the_role_reaches_the_system_prompt(tmp_path: Path, run: Any) -> None:
    import shutil

    agents = tmp_path / ".stcode" / "agents"
    agents.mkdir(parents=True)
    shutil.copy(EXAMPLE_AGENTS / "backend-dev.toml", agents / "backend-dev.toml")
    harness = run(
        Harness.create(
            tmp_path, role="backend-dev", load_mcp=False, load_repl=False, load_skills=False
        )
    )
    prompt = harness.system_prompt()
    assert "## Your role on this team" in prompt
    assert "api-contract.md" in prompt
    # And a sub-agent works inside the same role — it does not become role-less.
    assert "api-contract.md" in harness.for_subagent("worker").system_prompt()


def test_a_mounted_profile_needs_no_agents_directory(tmp_path: Path, run: Any) -> None:
    """The deployment shape: the profile *is* the config, so nothing is looked up.

    `-v ./agents/backend-dev.toml:/config/config.toml` is one mount, and the container
    has no agents directory at all. A harness that still went looking would refuse to
    start on exactly the deployment the profile format exists for.
    """
    harness = run(
        Harness.create(
            tmp_path,
            role="backend-dev",
            agent_prompt="# Role: backend developer\n\nYou own the API.",
            load_mcp=False,
            load_repl=False,
            load_skills=False,
        )
    )
    assert "You own the API." in harness.system_prompt()


def test_team_mode_is_off_until_it_is_switched_on(tmp_path: Path, run: Any) -> None:
    """Naming a role must not go looking for an inbox that is not mounted.

    The failure this prevents: a profile copied to a laptop used to start polling
    `/team` and refuse to run because the volume was not there.
    """
    from stcode.core.agent import Agent
    from stcode.core.configs import GatewayConfig

    config = GatewayConfig()
    config.agent.prompt = "# Role: tester\n\nYou run the suite."
    config.team.role = "tester"
    config.team.shared_dir = str(tmp_path / "not-mounted")
    config.supervisor.enabled = False
    config.mcp.enabled = False

    agent = run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]
    try:
        assert agent.mailbox is None, "a role alone turned team mode on"
        assert "send_message" not in agent.harness.tool_names()
        assert "You run the suite." in agent.harness.system_prompt()
    finally:
        run(agent.aclose())

    # And with the switch thrown, it joins — the volume is created on the way in.
    config.team.enabled = True
    agent = run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]
    try:
        assert agent.mailbox is not None
        assert "send_message" in agent.harness.tool_names()
    finally:
        run(agent.aclose())


def test_team_mode_with_no_role_refuses(tmp_path: Path, run: Any) -> None:
    """A mailbox with no owner would address every message to ""."""
    from stcode.core.agent import Agent
    from stcode.core.configs import GatewayConfig

    config = GatewayConfig()
    config.team.enabled = True
    config.supervisor.enabled = False
    config.mcp.enabled = False
    with pytest.raises(ValueError, match="has to be somebody"):
        run(Agent.create(config, cwd=tmp_path, gateway=_Scripted([])))  # type: ignore[arg-type]


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
    assert "send_message" in agent.harness.tool_names()
    assert "task" in agent.harness.tool_names()
    # But a sub-agent still cannot message another role: WORKER_TOOLS withholds it.
    assert "send_message" not in agent.harness.for_subagent("worker").tool_names()
    # And it knows who else is out there, without a registry.
    assert "ba" in agent.harness.teammates


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
