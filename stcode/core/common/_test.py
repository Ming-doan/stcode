"""
Common tests — the elision contract, which is CLAUDE.md §4 rule 1 in code.

Small module, but the one thing worth pinning is the thing that was wrong before: the
hint. `elide` used to name `tool_out["read_abc"]`, a dict in the harness process that
the model's REPL could not see, so a model that followed the instruction got a
`KeyError` (EXPECTED.md §4.1). The fix is not "a better variable name" — it is that
the caller passes a clause it is answerable for, and that the default clause names an
action (call again, narrower) rather than a location.
"""

from __future__ import annotations

from stcode.core.common import NARROW_REQUEST_HINT, ToolDefinition, ToolResult, elide
from stcode.core.common.truncate import DEFAULT_VIEW_LIMIT, HEAD_SHARE


def test_short_text_is_returned_untouched() -> None:
    assert elide("hello", 100) == "hello"
    assert elide("x" * 100, 100) == "x" * 100


def test_elision_keeps_head_and_tail_and_fits_the_budget() -> None:
    text = "HEAD" + ("m" * 5000) + "TAIL"
    out = elide(text, 500)

    assert len(out) <= 500
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "chars elided" in out
    # The tail is where a traceback's last line and a test summary live, so it keeps a
    # real share rather than a token one.
    assert 0 < len(out.split(" …\n")[-1]) < len(out) * (1 - HEAD_SHARE) + 40


def test_the_hint_names_an_action_the_model_can_actually_take() -> None:
    out = elide("z" * 5000, 200, hint=NARROW_REQUEST_HINT)
    assert NARROW_REQUEST_HINT in out
    # The old bug: pointing at a dict living in another process.
    assert "tool_out" not in out


def test_no_hint_still_marks_the_gap() -> None:
    out = elide("z" * 5000, 200)
    assert "chars elided]" in out and "—" not in out.split("\n")[1]


def test_degenerate_budgets_do_not_produce_a_marker_longer_than_the_text() -> None:
    assert elide("abc", 0) == ""
    tiny = elide("z" * 500, 10)
    assert len(tiny) <= 32  # clamped to _MIN_LIMIT rather than emitting a bare marker


def test_default_view_limit_is_the_documented_8192() -> None:
    assert DEFAULT_VIEW_LIMIT == 8192


def test_tool_result_error_carries_metadata_and_the_flag() -> None:
    result = ToolResult.error("nope", tool="read")
    assert result.is_error and result.content == "nope"
    assert result.metadata == {"tool": "read"}


def test_tool_definition_is_a_plain_schema_carrier() -> None:
    definition = ToolDefinition(name="read", description="Read a file.", input_schema={"type": "object"})
    assert definition.model_dump()["input_schema"] == {"type": "object"}
