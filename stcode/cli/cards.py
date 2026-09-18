"""
Cards — a list above the input, never a modal over the transcript.

Everything that used to be a dialog is one of these: the help, the command list, files
to mention, the mode and theme and effort pickers, an approval request, a question.

**Why not a modal.** A modal covers the transcript, which is the thing you need in
order to answer — *why* is it running this? A card leaves it on screen, and the turn
keeps streaming behind it.

**The input keeps focus.** A card is a list the keyboard drives from outside: the app
forwards ↑↓ and enter to it and everything else goes into the prompt, which is what
makes "type more to filter" work without a second text field to tab into.

`token_trigger` is the other half — which of `/` and `@` is being typed right now, and
what the filter is. `?` is not in there: it is a bare toggle on an empty input, not the
start of a token.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from stcode.cli.labels import Row
from stcode.cli.theme import brand_text

VISIBLE_ROWS = 6
"""How many rows a card shows before it scrolls. Six fits above the input on a short
terminal and is enough to see that there is more."""

LABEL_WIDTH = 13
"""Wide enough for `shift+enter`, so the descriptions line up in one column."""

TRIGGERS = ("/", "@")


def token_trigger(text: str, cursor: int) -> tuple[str, str] | None:
    """The `/` or `@` token the cursor is inside, as `(symbol, filter)`.

    A token starts at the beginning of the line or after whitespace, which is the whole
    rule: `src/api` is a path and `me@example.com` is an address, and neither should
    hijack the input. It ends at whitespace, so `/mode plan` is past the command.
    """
    if cursor <= 0 or cursor > len(text):
        return None
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:cursor]
    if not token or token[0] not in TRIGGERS:
        return None
    return token[0], token[1:]


def filter_rows(rows: Sequence[Row], query: str) -> list[Row]:
    """Narrow on a substring of the **label**, case-insensitively.

    The label only, and that is the interesting part. Searching the descriptions too
    looks generous and reads as noise: typing `/mod` would offer `/effort`, whose
    description happens to say "how hard the model should think". What you are typing
    is a name, so a name is what it matches — and `?` already lists all eleven with
    their descriptions for the times you do not know the name.

    Substring, not fuzzy. The fuzzy command palette was removed on purpose: two ways to
    reach the same eleven commands is one way for them to disagree.
    """
    if not query:
        return list(rows)
    needle = query.lower()
    return [row for row in rows if needle in row[0].lower()]


class Card(Vertical):
    """A titled list above the input. At most one is open at a time."""

    class Chosen(Message):
        """A row was picked. `kind` says which card, `token` carries a correlation id."""

        def __init__(self, kind: str, value: str, token: str = "") -> None:
            super().__init__()
            self.kind = kind
            self.value = value
            self.token = token

    def __init__(
        self,
        kind: str,
        rows: Sequence[Row],
        *,
        title: str = "",
        footer: str = "",
        body: str = "",
        token: str = "",
        highlighted: int = 0,
        selectable: bool = True,
    ) -> None:
        super().__init__(classes="card")
        self.kind = kind
        self.token = token
        """Correlation id for the request this card answers — an `execution_id` for an
        approval or a question, empty for everything else."""

        self.selectable = selectable
        self._title = title or kind
        self._footer = footer
        self._body = body
        self._rows = list(rows)
        self._highlighted = highlighted
        self._list = OptionList()

    # ---- layout ----

    def compose(self) -> ComposeResult:
        title = Text(self._title, style=f"bold {brand_text(self.app.current_theme.dark)}")
        header = Static(title, markup=False)
        header.add_class("card-title")
        yield header
        if self._body:
            body = Static(Text(self._body), markup=False)
            body.add_class("card-body")
            yield body
        yield self._list
        if self._footer:
            footer = Static(Text(self._footer, style="dim italic"), markup=False)
            footer.add_class("card-footer")
            yield footer

    def on_mount(self) -> None:
        self._fill(self._rows)

    # ---- content ----

    def show(self, rows: Sequence[Row]) -> None:
        """Replace the rows — what filtering does on every keystroke."""
        self._rows = list(rows)
        self._fill(self._rows)

    @property
    def rows(self) -> list[Row]:
        return list(self._rows)

    def _fill(self, rows: Iterable[Row]) -> None:
        self._list.clear_options()
        options = [Option(self._render_row(label, detail), id=label) for label, detail in rows]
        self._list.add_options(options)
        if options:
            self._list.highlighted = min(self._highlighted, len(options) - 1)
        # Height in rows, so the card is as tall as it needs to be up to the cap, and
        # the transcript keeps everything else.
        self._list.styles.height = min(max(len(options), 1), VISIBLE_ROWS)

    @staticmethod
    def _render_row(label: str, detail: str) -> Text:
        text = Text()
        text.append(label.ljust(LABEL_WIDTH) if detail else label, style="bold")
        if detail:
            text.append(f" {detail}", style="dim")
        return text

    # ---- driven from the prompt, which keeps focus ----

    def move(self, delta: int) -> None:
        if not self._rows:
            return
        current = self._list.highlighted or 0
        self._list.highlighted = max(0, min(current + delta, len(self._rows) - 1))

    def choose(self) -> bool:
        """Pick the highlighted row. False when there is nothing to pick, which is the
        signal to let ++enter++ mean "send" instead."""
        if not (self.selectable and self._rows):
            return False
        index = self._list.highlighted or 0
        self.post_message(self.Chosen(self.kind, self._rows[index][0], self.token))
        return True

    def choose_value(self, value: str) -> None:
        """Pick a named row directly — what `y` and `n` do on an approval card."""
        self.post_message(self.Chosen(self.kind, value, self.token))


class CardZone(Vertical):
    """The strip between the transcript and the input. Holds one card, or nothing."""

    def show(self, card: Card) -> Card:
        self.remove_children()
        self.mount(card)
        self.display = True
        return card

    def clear(self) -> None:
        self.remove_children()
        self.display = False

    @property
    def current(self) -> Card | None:
        cards = self.query(Card)
        return cards.first(Card) if cards else None


__all__ = ["LABEL_WIDTH", "TRIGGERS", "VISIBLE_ROWS", "Card", "CardZone", "filter_rows", "token_trigger"]
