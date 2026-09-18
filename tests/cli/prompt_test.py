"""
Prompt tests — the newline key, which is the one keystroke whose failure sends a
half-written message to a model.

++shift+enter++ and ++alt+enter++ only reach an application when the terminal speaks
the kitty keyboard protocol. Everywhere else the first arrives as a bare `CR` and the
second as `ESC CR`, and Textual reports both as `enter` — so on those terminals the two
documented newline keys *submit*. That is what these are here to stop happening again.
"""

from __future__ import annotations

from textual._xterm_parser import XTermParser

from stcode.cli.prompt import NEWLINE_KEYS


def _key_for(sequence: str) -> str:
    """What Textual calls the bytes a terminal sends. A fresh parser per call: it is
    stateful, and a leftover escape from the previous sequence changes the answer."""
    parser = XTermParser()
    events = [*parser.feed(sequence), *parser.feed("")]
    return events[0].key if events else ""


def test_ctrl_j_is_a_newline_key() -> None:
    """`LF`, a different byte from `CR`, on every terminal there is. The one that
    always works, and therefore the one the help card names first."""
    assert _key_for("\n") == "ctrl+j"
    assert "ctrl+j" in NEWLINE_KEYS


def test_the_kitty_spellings_are_covered() -> None:
    assert _key_for("\x1b[13;2u") == "shift+enter"
    assert _key_for("\x1b[13;3u") == "alt+enter"
    assert {"shift+enter", "alt+enter"} <= NEWLINE_KEYS


def test_the_modify_other_keys_spelling_is_covered() -> None:
    """xterm's own answer to the same problem. Textual names it after the character."""
    assert _key_for("\x1b[27;2;13~") == "shift+\r"
    assert "shift+\r" in NEWLINE_KEYS


def test_alt_enter_without_the_kitty_protocol_is_indistinguishable_from_enter() -> None:
    """Not a thing we can fix — a fact worth pinning, because it is the reason
    ++ctrl+j++ is the documented answer rather than a fallback nobody mentions."""
    assert _key_for("\x1b\r") == "enter"
    assert "enter" not in NEWLINE_KEYS, "enter must stay 'send'"
