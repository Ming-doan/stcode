"""
Labels — the copy, and the three places where it is doing work rather than wording.

An approval body that is not bounded pushes the answers off the screen; a `/` list that
does not carry the session's skills is a list you cannot run one from; a skill invoked
by name has to reach the agent as something it can act on.
"""

from __future__ import annotations

from stcode.cli import labels


# ---- what you are approving ------------------------------------------------------


def test_a_long_cell_keeps_its_head_and_its_tail() -> None:
    """A forty-line `repl` cell used to fill the card and leave deny/approve off the
    bottom of the screen."""
    code = "\n".join(f"line {index}" for index in range(40))
    body = labels.approval_summary("repl", "execute", {"code": code})

    lines = body.splitlines()[1:]  # the first line is `repl (execute)`
    assert len(lines) == labels.APPROVAL_BODY_LINES
    assert lines[0] == "line 0", "the head says what the cell is about to do"
    assert lines[-1] == "line 39", "the tail says what it leaves behind"
    assert "31 more lines" in body, "and the card says how much it is not showing"


def test_a_short_cell_is_shown_whole() -> None:
    body = labels.approval_summary("repl", "execute", {"code": "import os\nprint(os.getcwd())"})
    assert body == "repl (execute)\nimport os\nprint(os.getcwd())"


def test_a_command_is_still_shown_as_a_command() -> None:
    assert labels.approval_summary("bash", "execute", {"command": "ls -la"}).endswith("ls -la")


# ---- skills as commands ----------------------------------------------------------


def test_skills_are_offered_after_the_built_in_commands() -> None:
    rows = labels.commands_for(daemonless=False, skills=["pdf", "agent-browser"])
    names = [row[0] for row in rows]
    assert names[-2:] == ["/pdf", "/agent-browser"]
    assert "/model" in names


def test_a_skill_cannot_take_over_a_built_in_command() -> None:
    """`/clear` ends the session. A skill named `clear` must not quietly become the
    thing that runs instead."""
    rows = labels.commands_for(daemonless=False, skills=["clear"])
    assert [row[0] for row in rows].count("/clear") == 1
    assert dict(rows)["/clear"] == dict(labels.COMMANDS)["/clear"]


def test_a_skill_command_carries_the_rest_of_the_line_as_the_task() -> None:
    assert labels.skill_request("pdf", "split page 3 out") == 'Use the "pdf" skill. split page 3 out'
    assert labels.skill_request("pdf") == 'Use the "pdf" skill.'


def test_skill_command_spells_the_name_the_way_the_prompt_takes_it() -> None:
    assert labels.skill_command("agent-browser") == "/agent-browser"
    assert labels.skill_command("/agent-browser") == "/agent-browser"
