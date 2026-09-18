"""
Transcript tests — the five shapes, and the two bits of them that can be wrong
silently.

The rendering itself is Rich's problem. What is ours: a sub-agent that changes colour
between runs tells you nothing, and a thinking block that keeps everything defeats the
point of capping it.
"""

from __future__ import annotations

import io

from rich.console import Console

from stcode.cli.transcript import (
    AGENT_COLOURS,
    THINKING_LINES,
    agent_colour,
    format_arguments,
    shell_render,
    thinking_tail,
    tool_render,
)


def test_a_sub_agent_keeps_its_colour_across_runs() -> None:
    """Hashed from the name, not drawn at random: a colour that changes between
    sessions is decoration, and the point of it is recognition."""
    assert agent_colour("api-scout") == agent_colour("api-scout")
    assert agent_colour("api-scout") in AGENT_COLOURS
    # Different names should mostly differ — not a guarantee, but a bucket collision
    # on two of these would make the palette pointless.
    assert len({agent_colour(name) for name in ("api-scout", "test-writer", "docs")}) > 1


def test_arguments_read_as_the_tool_works_in() -> None:
    assert format_arguments({"path": "src/api/mw.py"}) == "path=src/api/mw.py"
    assert format_arguments({"a": 1, "b": "two"}) == "a=1 b=two"
    assert format_arguments({}) == ""


def test_a_long_argument_is_shortened_not_wrapped_forever() -> None:
    rendered = format_arguments({"cmd": "x" * 400})
    assert len(rendered) < 200 and rendered.endswith("…")


def test_a_multi_line_argument_becomes_one_line() -> None:
    """The first line of the box is a summary. A heredoc in `bash` must not push the
    result out of sight."""
    assert "\n" not in format_arguments({"cmd": "one\ntwo\nthree"})


def test_thinking_keeps_only_the_last_lines() -> None:
    text = "\n".join(f"line {index}" for index in range(20))
    kept = thinking_tail(text)
    assert kept.splitlines() == [f"line {index}" for index in range(20 - THINKING_LINES, 20)]


def test_thinking_shorter_than_the_cap_is_left_alone() -> None:
    assert thinking_tail("one\ntwo") == "one\ntwo"


def test_a_streaming_partial_line_still_counts_as_the_last_line() -> None:
    """Deltas arrive mid-line. The cap must not drop the line being written."""
    text = "\n".join(f"line {index}" for index in range(10)) + "\npartial"
    assert thinking_tail(text).endswith("partial")


# ---- the tool box -----------------------------------------------------------------


def _plain(renderable: object, width: int = 80) -> str:
    console = Console(width=width, file=io.StringIO(), no_color=True, legacy_windows=False)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[union-attr]


def test_the_tool_name_is_the_heading_and_carries_no_icon() -> None:
    """The colour says whether it worked. A `✗` on top of that is the same fact twice,
    and it shifts the name a character to the right on failures only."""
    ok = _plain(tool_render("read", {"path": "src/app.py"}))
    failed = _plain(tool_render("read", {"path": "nope.py"}, ok=False))
    assert "✗" not in ok and "✗" not in failed
    # The name is in the border row, not in a column of its own beside the arguments.
    assert ok.splitlines()[0].lstrip().startswith("╭─ read")
    assert "path=src/app.py" in ok


def test_a_tool_result_is_trimmed_to_two_lines() -> None:
    """Six calls each showing six lines is a screenful of output with the conversation
    pushed off the top. The whole value is in the session."""
    rendered = _plain(tool_render("grep", {"pattern": "x"}, result="a\nb\nc\nd\ne"))
    assert "a" in rendered and "b" in rendered
    assert "c" not in rendered.replace("╭", "").replace("╰", "")
    assert "…" in rendered


def test_a_short_result_is_not_marked_as_trimmed() -> None:
    rendered = _plain(tool_render("ls", {}, result="one\ntwo"))
    assert "one" in rendered and "two" in rendered and "…" not in rendered


def test_the_box_is_dim_until_the_call_finishes() -> None:
    """Running, worked, failed — three states, and only the last two are a verdict."""
    running = tool_render("bash", {"command": "make"}, finished=False)
    assert running.border_style == "dim"  # type: ignore[attr-defined]
    assert tool_render("bash", {}, ok=True).border_style == "green"  # type: ignore[attr-defined]
    assert tool_render("bash", {}, ok=False).border_style == "red"  # type: ignore[attr-defined]


# ---- the ! box --------------------------------------------------------------------


def test_a_shell_entry_shows_the_command_it_ran() -> None:
    rendered = _plain(shell_render("git status", "nothing to commit"))
    assert "! git status" in rendered
    assert "nothing to commit" in rendered


def test_a_non_zero_exit_says_so() -> None:
    assert "(exit 3)" in _plain(shell_render("false", "", exit_code=3))


def test_a_flood_of_output_is_bounded() -> None:
    """`!cat` on the wrong file should not be how a session scrolls away."""
    rendered = _plain(shell_render("cat big", "\n".join(str(n) for n in range(500))))
    assert "more lines, not shown" in rendered
