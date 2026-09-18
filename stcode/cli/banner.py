"""
Banner — the ASCII wordmark, centred on an empty transcript and gone once there is a
conversation.

It is a greeting, not furniture: the screen exists for the conversation, and a wordmark
that stays pinned to the top of a long one is six rows of nothing. `Banner.follow()`
takes the decision from the transcript rather than counting messages — "does it scroll
yet" is the honest test of "is this long", at any terminal height.

Lines are padded to a common width at import time rather than carrying trailing spaces
in the source (linters strip those), because `text-align: center` centers each line
independently and ragged lines render as a crooked wordmark.
"""

from __future__ import annotations

from textual.events import Resize
from textual.widgets import Static

_FULL_LINES = [
    "███████╗████████╗ ██████╗ ██████╗ ██████╗ ███████╗",
    "██╔════╝╚══██╔══╝██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    "███████╗   ██║   ██║     ██║   ██║██║  ██║█████╗",
    "╚════██║   ██║   ██║     ██║   ██║██║  ██║██╔══╝",
    "███████║   ██║   ╚██████╗╚██████╔╝██████╔╝███████╗",
    "╚══════╝   ╚═╝    ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
]

_COMPACT_LINES = [
    "╔═╗╔╦╗╔═╗╔═╗╔╦╗╔═╗",
    "╚═╗ ║ ║  ║ ║ ║║║╣",
    "╚═╝ ╩ ╚═╝╚═╝═╩╝╚═╝",
]


def _padded(lines: list[str]) -> str:
    width = max(len(line) for line in lines)
    return "\n".join(line.ljust(width) for line in lines)


BANNER_FULL = _padded(_FULL_LINES)
BANNER_COMPACT = _padded(_COMPACT_LINES)

BANNER_FULL_WIDTH = max(len(line) for line in _FULL_LINES)


def banner_for_width(width: int) -> str:
    """The widest wordmark that fits, so a narrow terminal degrades instead of clipping."""
    return BANNER_FULL if width >= BANNER_FULL_WIDTH + 4 else BANNER_COMPACT


class Banner(Static):
    """The wordmark, swapped for a compact one when the terminal gets narrow."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 — textual's name
        super().__init__("", id=id, markup=False)

    def on_mount(self) -> None:
        self._render_for(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._render_for(event.size.width)

    def follow(self, *, transcript_scrolls: bool) -> None:
        """Hide once there is more transcript than screen."""
        self.display = not transcript_scrolls

    def _render_for(self, width: int) -> None:
        self.update(banner_for_width(width))
