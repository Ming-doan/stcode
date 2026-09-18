"""
Card tests — when a symbol opens a card, and what typing more does to it.

Three characters mean something in the input: `?`, `/` and `@`. Getting the *when*
wrong is the bug people notice: a `/` inside a path that hijacks the input, or an `@`
in an email address that opens a file picker.
"""

from __future__ import annotations

from stcode.cli.cards import filter_rows, token_trigger
from stcode.cli.labels import COMMANDS


def test_a_slash_at_the_start_opens_the_command_list() -> None:
    assert token_trigger("/", 1) == ("/", "")
    assert token_trigger("/mo", 3) == ("/", "mo")


def test_a_slash_after_a_space_opens_it_too() -> None:
    assert token_trigger("look at /mo", 11) == ("/", "mo")


def test_a_slash_inside_a_word_is_just_a_slash() -> None:
    """`src/api` and `and/or` are text. A path must not hijack the input."""
    assert token_trigger("src/api", 7) is None
    assert token_trigger("read src/api/mw.py", 18) is None


def test_an_at_sign_mid_word_is_just_an_at_sign() -> None:
    """An email address is not a file mention."""
    assert token_trigger("me@example.com", 14) is None
    assert token_trigger("see @src/api", 12) == ("@", "src/api")


def test_a_token_ends_at_a_space() -> None:
    """`/mode plan` is past the command: the cursor is in `plan`, not in `/mode`."""
    assert token_trigger("/mode plan", 10) is None


def test_the_cursor_decides_which_token_is_being_typed() -> None:
    assert token_trigger("/mode and @file", 5) == ("/", "mode")
    assert token_trigger("/mode and @file", 15) == ("@", "file")


def test_nothing_in_an_empty_input() -> None:
    assert token_trigger("", 0) is None
    assert token_trigger("plain words", 11) is None


def test_filtering_narrows_on_the_label() -> None:
    rows = [("/model", "provider, key and model"), ("/mode", "approval mode"), ("/quit", "leave")]
    assert [row[0] for row in filter_rows(rows, "mod")] == ["/model", "/mode"]
    assert filter_rows(rows, "") == rows
    assert filter_rows(rows, "zzz") == []


def test_filtering_does_not_search_the_descriptions() -> None:
    """Typing `/mod` must not offer `/effort`, whose description says "how hard the
    model should think". What you are typing is a name."""
    rows = [("/effort", "how hard the model should think"), ("/mode", "approval mode")]
    assert [row[0] for row in filter_rows(rows, "mod")] == ["/mode"]


def test_filtering_ignores_case() -> None:
    rows = [("/Model", "x")]
    assert filter_rows(rows, "mod") == rows


def test_every_documented_command_exists_exactly_once() -> None:
    """The guide lists twelve and says they are the whole surface. A thirteenth that
    only exists in code is a command nobody can find."""
    names = [name for name, _ in COMMANDS]
    assert names == sorted(set(names), key=names.index), "a command is listed twice"
    assert set(names) == {
        "/model",
        "/effort",
        "/mode",
        "/theme",
        "/token",
        "/sessions",
        "/connect",
        "/mcp",
        "/skills",
        "/clear",
        "/help",
        "/quit",
    }


def test_connect_is_offered_only_where_it_means_something() -> None:
    """`stcode` on its own started the daemon it is talking to. Offering to move the
    terminal off it would leave that daemon running with nothing attached."""
    from stcode.cli.labels import commands_for

    assert "/connect" in [name for name, _ in commands_for(daemonless=True)]
    assert "/connect" not in [name for name, _ in commands_for(daemonless=False)]
    # Nothing else changes between the two.
    assert len(commands_for(daemonless=True)) == len(commands_for(daemonless=False)) + 1
