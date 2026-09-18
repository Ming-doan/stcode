"""
Transcript tests — the five shapes, and the two bits of them that can be wrong
silently.

The rendering itself is Rich's problem. What is ours: a sub-agent that changes colour
between runs tells you nothing, and a thinking block that keeps everything defeats the
point of capping it.
"""

from __future__ import annotations

from stcode.cli.transcript import (
    AGENT_COLOURS,
    THINKING_LINES,
    agent_colour,
    format_arguments,
    thinking_tail,
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
