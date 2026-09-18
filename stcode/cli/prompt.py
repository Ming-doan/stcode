"""
The prompt — a text area that keeps focus while a card is open.

Two jobs beyond editing text:

**Send on enter, newline on ctrl+j.** A `TextArea` does the opposite by default, and the
default is wrong here: sending is the common act.

The newline key is the part worth being careful about. ++shift+enter++ and
++alt+enter++ only exist on the wire when the terminal speaks the **kitty keyboard
protocol** — Textual asks for it on start-up, and a terminal that does not answer sends
plain `CR` for shift+enter and `ESC CR` for alt+enter, both of which arrive here as a
bare `enter`. So on those terminals the two documented newline keys *send the message*,
which is the worst possible failure for a key whose job is to not send it.

++ctrl+j++ is the one that always works: it is `LF`, a different byte from `CR`, on
every terminal there is. It is the documented answer; the other two are kept for the
terminals that do report them, along with the `modifyOtherKeys` spellings xterm uses.

**Driving the open card without giving up focus.** ↑↓, enter, escape and — on an empty
input — backspace are handed to the app while a card is open; everything else is
editing, and the app re-reads the token afterwards to filter. That is what makes "type
more to filter" work with no second field to tab into.

`?` is the one character that does not insert itself: on an empty input it opens the
help card instead. Backspace closes that card, and because nothing was inserted, typing
`?` again inserts it — which is what you meant the second time.
"""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea

MAX_ROWS = 8
"""How tall the input grows before it scrolls. Past this, you are writing a document,
and the transcript is worth more screen than the draft."""

NEWLINE_KEYS = frozenset(
    {
        "ctrl+j",
        "shift+enter",
        "alt+enter",
        # `modifyOtherKeys`, which xterm and a few others speak instead of the kitty
        # protocol. Textual names these after the character rather than the key.
        "shift+\r",
        "alt+\r",
    }
)
"""Every spelling of "newline, do not send". ++ctrl+j++ is the one that works
everywhere; the rest depend on what the terminal reports. → this module's docstring."""


class Prompt(TextArea):
    """The input line. Grows to `MAX_ROWS`, then scrolls."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+j", "newline", "New line", show=False),
        Binding("alt+enter", "newline", "New line", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class HelpRequested(Message):
        """`?` on an empty input."""

    class CardMove(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class CardChoose(Message):
        """++enter++ with a card open. The app falls back to sending if nothing is
        highlighted, so this never swallows a message."""

    class CardClose(Message):
        """++escape++, or ++backspace++ on an empty input, with a card open."""

    class CardHotkey(Message):
        """A single key the open card claimed — ++y++ and ++n++ on an approval."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Interrupted(Message):
        """++escape++ with no card open."""

    class ModeCycle(Message):
        """++shift+tab++. Handled here rather than as an app binding because a focused
        `TextArea` claims tab and shift+tab for focus movement before one is reached,
        and the prompt has focus the entire time the app is usable."""

    def __init__(self, placeholder: str = "") -> None:
        super().__init__(
            id="prompt", soft_wrap=True, show_line_numbers=False, placeholder=placeholder
        )
        self.card_open = False
        """Set by the app. The prompt does not know what a card *is* — only that its
        arrow keys belong to something else while one is up."""

        self.card_hotkeys: dict[str, str] = {}
        """Single keys the open card has claimed, as key -> the row it picks. Empty for
        every card but an approval: a letter that silently means something is a letter
        you cannot type, so only a card that says so up front gets one."""

    # ---- what the app needs to filter on ----

    @property
    def cursor_offset(self) -> int:
        """The cursor as an index into `text`, which is what `token_trigger` wants."""
        return self.document.get_index_from_location(self.cursor_location)

    def clear_text(self) -> str:
        """Empty the input and hand back what was in it."""
        text = self.text
        self.text = ""
        return text

    def insert_token(self, symbol: str, value: str) -> None:
        """Replace the `symbol`-token at the cursor with `value` — how a chosen file
        mention lands. The symbol goes too: the path is the payload."""
        offset = self.cursor_offset
        start = offset
        while start > 0 and not self.text[start - 1].isspace():
            start -= 1
        before, after = self.text[:start], self.text[offset:]
        self.text = f"{before}{value} {after}"
        self.move_cursor(self.document.get_location_from_index(len(before) + len(value) + 1))

    # ---- keys ----

    def action_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: Key) -> None:
        """Intercept before `TextArea`'s own bindings see it.

        `_on_key` rather than `on_key`: `TextArea` binds ++enter++ to insert a newline,
        and a handler that runs after that binding is a handler that runs too late.
        """
        key = event.key
        if self.card_open:
            if key in ("up", "down"):
                event.prevent_default()
                event.stop()
                self.post_message(self.CardMove(-1 if key == "up" else 1))
                return
            if key == "enter":
                event.prevent_default()
                event.stop()
                self.post_message(self.CardChoose())
                return
            if key == "escape" or (key == "backspace" and not self.text):
                event.prevent_default()
                event.stop()
                self.post_message(self.CardClose())
                return
            if key in self.card_hotkeys:
                event.prevent_default()
                event.stop()
                self.post_message(self.CardHotkey(self.card_hotkeys[key]))
                return

        if key == "question_mark" and not self.text and not self.card_open:
            event.prevent_default()
            event.stop()
            self.post_message(self.HelpRequested())
            return

        if key == "shift+tab":
            event.prevent_default()
            event.stop()
            self.post_message(self.ModeCycle())
            return

        if key in NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        if key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return

        if key == "escape":
            event.prevent_default()
            event.stop()
            self.post_message(self.Interrupted())
            return

        await super()._on_key(event)


__all__ = ["MAX_ROWS", "NEWLINE_KEYS", "Prompt"]
