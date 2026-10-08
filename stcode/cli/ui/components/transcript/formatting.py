"""
The transcript — eight shapes, each one because it has to be told apart at a glance.

| | |
| --- | --- |
| `───── attached to … ─────` | the platform acted. Not the model talking |
| a tinted block | what **you** said |
| `│ quoted, dim` | thinking. Capped at the last six lines while it streams |
| dim, unquoted | a running tool's output. Advisory, never in the session |
| plain text | the model's answer |
| a green or red box | a tool call and its result — the colour *is* the outcome |
| a rule down the left | a `!` command you ran. Never in the session |
| a tinted full-width box | a warning or an error |

A sub-agent renders in exactly the same shapes, with its name in a left gutter and its
content indented. The colour is **hashed from the name**, not drawn at random: the same
sub-agent is the same colour in every session and after every restart, which is the only
way the colour tells you anything.

Nothing here parses markup. Every string in this module came from a model, a file or a
shell, and a stray `[` in one must not be read as a style tag.
"""

from __future__ import annotations

import zlib
from typing import Any, Mapping

from rich.console import RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich import box


THINKING_LINES = 6
"""How much reasoning stays on screen while it streams. Six is what fits without the
answer sliding off the bottom; the rest is scrollable, and all of it is in the session."""

TOOL_RESULT_LINES = 2
"""Lines of a tool result kept in the box.

Two, because the box is a *receipt* and not a viewer: the interesting thing about a
finished tool call is that it ran and whether it worked, and a turn with six calls each
showing six lines is a screen of output with the conversation pushed off the top. The
full value is in the session, and `ToolFinished` only carries 240 characters of it
anyway.
"""

PROGRESS_LINES = 6
"""Lines of a running tool's output kept on screen.

The same six as `Thinking`, for the same reason: it is a sign of life, not a viewer.
The tool's result is what gets a box, and the whole value is in the session.
"""

SHELL_OUTPUT_LINES = 200
"""Lines of a `!` command's output kept on screen. Generous, because you asked for this
one by hand — but bounded, because `!cat` on the wrong file should not be how a session
scrolls away."""

ARGUMENT_CHARS = 160

AGENT_COLOURS = (
    "cyan",
    "magenta",
    "yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
    "orange3",
    "spring_green3",
)
"""Eight, none of them the brand green — that one means *the platform*, and a sub-agent
wearing it would read as one."""


def agent_colour(name: str) -> str:
    """A stable colour for a sub-agent name.

    `crc32` rather than `hash()`: Python salts string hashing per process, so `hash()`
    would give the same agent a different colour every time stcode starts.
    """
    return AGENT_COLOURS[zlib.crc32(name.encode("utf-8")) % len(AGENT_COLOURS)]


def format_arguments(arguments: Mapping[str, Any]) -> str:
    """A tool's arguments as one line, in the terms the tool works in.

    One line, because it is the summary at the top of the box: a `bash` heredoc that
    wrapped over twelve rows would push the result out of sight.
    """
    rendered = " ".join(f"{key}={_flatten(value)}" for key, value in arguments.items())
    return _shorten(rendered, ARGUMENT_CHARS)


def thinking_tail(text: str, limit: int = THINKING_LINES) -> str:
    """The last `limit` lines. Deltas arrive mid-line, so the line being written counts."""
    lines = text.splitlines() or [text]
    return "\n".join(lines[-limit:])


def _flatten(value: Any) -> str:
    return " ".join(str(value).split())


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---- renderables ----------------------------------------------------------------


def platform_rule(text: str) -> RenderableType:
    """`───── mode → auto-edit ─────`, full width, dim italic.

    A rule rather than a line of text: what the *platform* did is not part of the
    conversation, and it should not be possible to mistake one for the other.

    Flattened to one line first. Rich draws a rule **per line** of its title, so a
    multi-line string here comes out as a screen of dashes with a word centred in each
    — which is what a tool's output looked like when it arrived as progress.
    """
    return Rule(
        Text(" ".join(text.split()), style="italic dim"), characters="─", style="dim"
    )


def tool_render(
    name: str,
    arguments: Mapping[str, Any],
    *,
    result: str = "",
    ok: bool = True,
    finished: bool = True,
    colour: str = "",
) -> RenderableType:
    """One tool call as a small box: its name in the heading, arguments and result under.

    **The colour carries the outcome, so nothing else has to.** Green when it worked,
    red when it did not, dim while it is still running — which means no `✗` to read, no
    second word for the same fact, and a turn you can skim by colour alone. The name is
    the box's heading rather than a first column, so a long `bash` command no longer
    decides how wide the name column is.

    `colour` is the theme's, resolved by the caller: a Rich style string cannot say
    `$success`. Left empty it falls back to the plain ANSI names, which is what tests
    and any non-Textual caller get.
    """
    if not colour:
        colour = "dim" if not finished else ("green" if ok else "red")

    body = Text(format_arguments(arguments), style="dim")
    for line in _result_lines(result):
        body.append("\n")
        body.append(line, style="dim")
    return Panel(
        body,
        title=Text(name, style=f"bold {colour}"),
        title_align="left",
        border_style=colour,
        box=box.ROUNDED,
        padding=(0, 1),
        expand=True,
    )


def _result_lines(result: str, limit: int = TOOL_RESULT_LINES) -> list[str]:
    if not result.strip():
        return []
    lines = [line.rstrip() for line in result.strip().splitlines()]
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], "…"]


def shell_render(command: str, output: str, *, exit_code: int = 0) -> RenderableType:
    """A `!` command and what it printed. The command bold, the output plain.

    No border of its own — the widget draws the left rule in CSS, where it can be the
    theme's colour. This is only the contents.
    """
    text = Text()
    text.append(f"! {command}", style="bold")
    if exit_code:
        text.append(f"  (exit {exit_code})", style="bold")
    lines = output.rstrip().splitlines()
    if len(lines) > SHELL_OUTPUT_LINES:
        hidden = len(lines) - SHELL_OUTPUT_LINES
        lines = [*lines[:SHELL_OUTPUT_LINES], f"… {hidden} more lines, not shown"]
    for line in lines:
        text.append("\n")
        text.append(line.rstrip())
    return text


# ---- widgets --------------------------------------------------------------------
