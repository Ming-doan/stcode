"""
Themes — the project's green, in the terminal's own light or dark.

Two themes, one primary colour (`#78b032`). The default preference is `auto`, and
`auto` is ours to implement: Textual 8 detects the terminal's background nowhere, so
this asks.

Three sources, cheapest first:

1. **`COLORFGBG`** — exported by a handful of terminals, free to read, and an answer
   without a round trip.
2. **`OSC 11`** — write `ESC ] 11 ; ? BEL`, read back `rgb:rrrr/gggg/bbbb`. Works in
   most modern terminals, and the ones where it does not simply say nothing.
3. **Dark.** Not a guess about your taste; a guess about terminals, most of which are.

The budget is the part that matters. A terminal that ignores the query must cost 200ms
once, never a UI that will not start — so the read is bounded by `select`, the tty is
put back exactly as it was, and every failure path returns rather than raises.

Detection runs **before** the app starts: Textual owns the tty afterwards, and two
things reading raw escape sequences off the same terminal is a race with a corrupted
screen at the end of it.
"""

from __future__ import annotations

import os
import re
import select
import sys
from typing import Literal

from textual.theme import Theme

from stcode.cli.prefs import ThemePreference

PRIMARY = "#78b032"
"""stcode's green. The one colour the UI is built out of."""

TerminalMode = Literal["dark", "light"]

QUERY = "\x1b]11;?\x07"
REPLY = re.compile(r"rgb:([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})")
TIMEOUT = 0.2
"""Long enough for a terminal on the other end of an ssh hop, short enough that a
terminal which will never answer costs one fifth of a second, once."""

BRAND_TEXT_ON_LIGHT = "#3f6618"
"""The green, darkened enough to be *read* on the light ground.

`primary` stays `#78b032` in both themes, as the brand does. But #78b032 on an off-white
background is about 2.6:1 — fine for a border, unreadable as a sentence — so anything
writing words asks `brand_text()` instead. In CSS the equivalent is `$text-primary`,
which Textual derives the same way; this constant exists because a Rich style string is
not CSS and cannot reference a variable.
"""

STCODE_DARK = Theme(
    name="stcode-dark",
    primary=PRIMARY,
    secondary="#4f7d1f",
    accent=PRIMARY,
    warning="#d7a12c",
    error="#d2544b",
    success=PRIMARY,
    foreground="#d6d6d2",
    background="#101210",
    surface="#191c19",
    panel="#20241f",
    dark=True,
)

STCODE_LIGHT = Theme(
    name="stcode-light",
    primary=PRIMARY,
    secondary=BRAND_TEXT_ON_LIGHT,
    accent=PRIMARY,
    warning="#8a6a00",
    error="#b3261e",
    success=BRAND_TEXT_ON_LIGHT,
    foreground="#1c1f1b",
    background="#fbfbf8",
    surface="#f2f3ee",
    panel="#e8eae2",
    dark=False,
)

THEMES = (STCODE_DARK, STCODE_LIGHT)


def brand_text(dark: bool) -> str:
    """The brand colour, in the version that can be read as text on this ground."""
    return PRIMARY if dark else BRAND_TEXT_ON_LIGHT


def theme_name_for(preference: ThemePreference, *, detected: TerminalMode) -> str:
    """Which registered theme to use. An explicit choice always beats detection."""
    if preference == "dark":
        return STCODE_DARK.name
    if preference == "light":
        return STCODE_LIGHT.name
    return STCODE_LIGHT.name if detected == "light" else STCODE_DARK.name


def background_is_light(reply: str) -> bool | None:
    """Read an `OSC 11` reply. None when it is not one — which is not a failure.

    Perceived luminance, not the mean: a saturated blue background is dark with one
    channel at full, and a mid yellow is light with one channel at zero.
    """
    match = REPLY.search(reply or "")
    if match is None:
        return None
    try:
        channels = [int(value, 16) / (16 ** len(value) - 1) for value in match.groups()]
    except (ValueError, ZeroDivisionError):
        return None
    red, green, blue = channels
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) > 0.5


def mode_from_colorfgbg(value: str | None) -> TerminalMode | None:
    """`COLORFGBG` as some terminals export it: `"<fg>;<bg>"`, ANSI indices.

    The background is the last field. 0-6 and 8 are the dark half of the base palette;
    `default` means the terminal's own, which for the terminals that set this variable
    at all is light.
    """
    if not value:
        return None
    background = value.split(";")[-1].strip().lower()
    if background == "default":
        return "light"
    if not background.isdigit():
        return None
    return "dark" if int(background) in (0, 1, 2, 3, 4, 5, 6, 8) else "light"


def detect_terminal_mode(timeout: float = TIMEOUT) -> TerminalMode:
    """The terminal's background, as far as it will say. Dark when it will not."""
    from_env = mode_from_colorfgbg(os.environ.get("COLORFGBG"))
    if from_env is not None:
        return from_env
    reply = _query_background(timeout)
    is_light = background_is_light(reply) if reply else None
    if is_light is None:
        return "dark"
    return "light" if is_light else "dark"


def _query_background(timeout: float) -> str:
    """Ask the tty directly and read whatever comes back inside `timeout`.

    Raw mode for the duration and restored in a `finally`: leaving a terminal without
    echo is worse than picking the wrong theme, and this runs before Textual has taken
    the tty over. Every failure — no tty, no termios (Windows), a terminal that writes
    nothing — returns the empty string, which the caller reads as "no answer".
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ""
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover — Windows
        return ""

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return ""
    try:
        tty.setraw(fd, termios.TCSANOW)
        sys.stdout.write(QUERY)
        sys.stdout.flush()
        chunks: list[str] = []
        deadline_left = timeout
        while deadline_left > 0:
            ready, _, _ = select.select([fd], [], [], deadline_left)
            if not ready:
                break
            chunks.append(os.read(fd, 64).decode("utf-8", errors="replace"))
            text = "".join(chunks)
            # Stop at the terminator the reply actually used — BEL or ST — rather than
            # waiting out the whole budget on a terminal that already answered.
            if text.endswith("\x07") or text.endswith("\x1b\\") or REPLY.search(text):
                break
            deadline_left = timeout / 4
        return "".join(chunks)
    except (OSError, ValueError):
        return ""
    finally:
        with_suppressed_error = getattr(termios, "error", Exception)
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except with_suppressed_error:  # pragma: no cover
            pass


__all__ = [
    "BRAND_TEXT_ON_LIGHT",
    "PRIMARY",
    "QUERY",
    "STCODE_DARK",
    "STCODE_LIGHT",
    "THEMES",
    "TIMEOUT",
    "TerminalMode",
    "background_is_light",
    "brand_text",
    "detect_terminal_mode",
    "mode_from_colorfgbg",
    "theme_name_for",
]
