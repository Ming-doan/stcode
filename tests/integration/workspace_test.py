"""
What an agent loads from the directory it is pointed at.

A session's behaviour is decided as much by the project as by the config: a role file,
a `SKILL.md`, an `AGENTS.md`, an `.mcp.json`. Each of those is discovered by a separate
piece of code with its own search path, and each one silently discovering *nothing* is
indistinguishable, from the outside, from a model that ignored its instructions.

So these mount `tests/fixtures/workspace/` as the agent's cwd and check that what is on
disk reached the prompt — and, for MCP, that it reached the prompt as *code*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fakes import fake_provider, says

from stcode.core.agent import Agent
from stcode.core.configs import load_config
from stcode.core.harness import Harness
from stcode.core.harness.mcp import MCP_CODE_DIRNAME


def test_the_config_file_in_the_project_loads(config_path: Path) -> None:
    """`config.toml` is the file every other test here builds on, so it is checked
    first: a typo in it would fail every test below with an unrelated message."""
    config = load_config(config_path)

    assert config.defaults.provider == "fake"
    assert config.routing["high"].model == "fake-large"
    assert config.agent.max_turns == 12
    assert config.supervisor.every == 4


def test_a_harness_picks_up_role_skills_and_project_instructions(
    workspace: Path, run: Any
) -> None:
    """Three separate search paths, one assertion each — because a search path that
    finds nothing looks exactly like a model that did not read what it found."""
    harness = run(
        Harness.create(
            cwd=workspace, role="backend-dev", approval_mode="auto-edit", load_mcp=False
        )
    )
    try:
        prompt = harness.system_prompt()

        # .stcode/agents/backend-dev.toml
        assert "Role: backend developer" in prompt
        assert "Message devops instead." in prompt

        # .agents/skills/*/SKILL.md — the catalogue, not the bodies. Progressive
        # disclosure is the whole design, so finding a skill's *instructions* in the
        # prompt would be the bug.
        assert "release-notes" in prompt and "db-migrations" in prompt
        assert "Write the `down` first" not in prompt
        # And *only* the project's skills: `isolated_home` is what stops this
        # assertion depending on what the developer has in `~/.agents/skills`.
        assert [skill.name for skill in harness.context.skills or []] == [
            "db-migrations",
            "release-notes",
        ]

        # AGENTS.md
        assert "Money is integer cents everywhere" in prompt
    finally:
        run(harness.aclose())


def test_a_skill_body_loads_only_when_asked_for(workspace: Path, run: Any) -> None:
    harness = run(Harness.create(cwd=workspace, approval_mode="auto-edit", load_mcp=False))
    try:
        result = run(harness.invoke("skill", {"name": "db-migrations"}))
        assert not result.is_error
        assert "Write the `down` first" in result.content
    finally:
        run(harness.aclose())


def test_mcp_servers_arrive_as_code_not_as_tool_definitions(
    mcp_workspace: Path, run: Any
) -> None:
    """Bet 3.1, end to end from a real `.mcp.json`.

    The prompt learns that a server exists and where its stubs are; the schemas — the
    expensive part — go to disk, where a `grep` reaches them and the turn-by-turn
    prefix does not carry them.
    """
    harness = run(Harness.create(cwd=mcp_workspace, approval_mode="auto-edit"))
    try:
        generated = mcp_workspace / MCP_CODE_DIRNAME / "ledger"
        assert (generated / "lookup_account.py").is_file()
        assert (generated / "list_branches.py").is_file()

        # The argument documentation is in the file, which is the point of writing one.
        stub = (generated / "lookup_account.py").read_text()
        assert "include_closed" in stub

        prompt = harness.system_prompt()
        assert "ledger" in prompt and "lookup_account" in prompt
        # Names, not schemas. `include_closed` is a per-turn cost in `tools` mode and
        # a grep in `code` mode; this is the assertion that says which mode ran.
        assert "include_closed" not in prompt
        assert "mcp__ledger__lookup_account" not in harness.tool_names()
    finally:
        run(harness.aclose())


def test_an_agent_built_from_the_config_sends_what_the_workspace_holds(
    workspace: Path, config_path: Path, run: Any
) -> None:
    """The whole assembly, through `Agent.create`, with only the SDK replaced.

    This is the test that would catch a refactor which quietly stops passing the role,
    the tools or the routed model down to the wire.
    """
    with fake_provider([says("Read it.")]) as fake:
        config = load_config(config_path)
        config.session.dir = workspace / ".stcode" / "sessions"
        agent = run(
            Agent.create(config, cwd=workspace, role="backend-dev", load_mcp=False)
        )
        try:
            run(_drain(agent, "What does src/util.py do?"))
        finally:
            run(agent.aclose())

    request = fake.last
    # `[agent] difficulty = "high"` routed to the `high` tier's model.
    assert request.model == "fake-large"
    assert "Role: backend developer" in (request.system or "")
    assert {"read", "edit", "grep", "bash"} <= set(request.tool_names)
    assert request.texts[-1] == "What does src/util.py do?"


async def _drain(agent: Agent, text: str) -> list[Any]:
    return [event async for event in agent.run(text)]
