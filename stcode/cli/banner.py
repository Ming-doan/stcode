"""
Banner — the ASCII wordmark at the top of the chat screen.

Lines are padded to a common width at import time rather than carrying trailing spaces
in the source (linters strip those), because `text-align: center` centers each line
independently and ragged lines render as a crooked wordmark.
"""

from __future__ import annotations

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
