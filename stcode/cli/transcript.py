"""
The transcript — five shapes, each one because it has to be told apart at a glance.

| | |
| --- | --- |
| `───── attached to … ─────` | the platform acted. Not the model talking |
| `│ quoted, dim` | thinking. Capped at the last six lines while it streams |
| plain text | the model's answer |
| a box | a tool call and its result |
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
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from stcode.cli.theme import brand_text

THINKING_LINES = 6
"""How much reasoning stays on screen while it streams. Six is what fits without the
answer sliding off the bottom; the rest is scrollable, and all of it is in the session."""

TOOL_RESULT_LINES = 6
"""Lines of a tool result kept in the box. The full value is in the session, and
`ToolFinished` only carries 240 characters of it anyway."""

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
    """
    return Rule(Text(text, style="italic dim"), characters="─", style="dim")


def tool_render(
    name: str, arguments: Mapping[str, Any], *, result: str = "", ok: bool = True
) -> RenderableType:
    """One tool call as a box: the name, a gutter, the arguments, then the result.

    A table with a border and two columns, so the gutter is the column divider rather
    than characters counted by hand at every terminal width.
    """
    table = Table(
        box=box.ROUNDED,
        show_header=False,
        expand=True,
        padding=(0, 1),
        border_style="red" if not ok else "dim",
    )
    table.add_column(no_wrap=True, justify="left", style="bold" if ok else "bold red")
    table.add_column(overflow="fold", ratio=1)
    table.add_row(Text(name if ok else f"✗ {name}"), Text(format_arguments(arguments)))
    for line in _result_lines(result):
        table.add_row(Text(""), Text(line, style="" if ok else "red"))
    return table


def _result_lines(result: str) -> list[str]:
    if not result.strip():
        return []
    lines = [line.rstrip() for line in result.strip().splitlines()]
    if len(lines) <= TOOL_RESULT_LINES:
        return lines
    hidden = len(lines) - TOOL_RESULT_LINES
    return [*lines[:TOOL_RESULT_LINES], f"… {hidden} more line{'s' if hidden > 1 else ''}"]


# ---- widgets --------------------------------------------------------------------


class Message(Static):
    """One block of text — the user's, or the model's answer.

    `you` is labelled and the model is not: on a screen where one side is the one
    typing, only one of them needs saying.
    """

    def __init__(self, role: str, body: str = "") -> None:
        super().__init__(classes=f"entry message {role}", markup=False)
        self._role = role
        self.body = body

    def on_mount(self) -> None:
        self._refresh()

    def append(self, text: str) -> None:
        self.body += text
        self._refresh()

    def _refresh(self) -> None:
        if self._role == "user":
            text = Text()
            # Resolved here rather than written as `$text-primary`: a Rich style string
            # is not CSS and cannot reference a theme variable.
            text.append("you  ", style=f"bold {brand_text(self.app.current_theme.dark)}")
            text.append(self.body.replace("\n", "\n     "))
            self.update(text)
        else:
            self.update(Text(self.body))


class Thinking(VerticalScroll):
    """Reasoning, quoted and dim, showing the last six lines and scrollable.

    Its own scroller rather than a growing block: reasoning can be longer than the
    answer, and a model that thought for two pages must not push the answer it then
    gave off the screen.
    """

    def __init__(self, body: str = "") -> None:
        super().__init__(classes="entry thinking")
        self.body = body
        self._text = Static(markup=False)

    def compose(self) -> Any:
        yield self._text

    def on_mount(self) -> None:
        self._refresh()

    def append(self, text: str) -> None:
        self.body += text
        self._refresh()

    def _refresh(self) -> None:
        quoted = "\n".join(f"│ {line}" for line in thinking_tail(self.body).splitlines())
        self._text.update(Text(quoted, style="italic dim"))
        self.scroll_end(animate=False)


class ToolCall(Static):
    """A tool call, updated in place when its result arrives.

    In place, because `tool_started` and `tool_finished` are the same event to a
    reader: two boxes for one call would double the transcript and say nothing more.
    """

    def __init__(self, name: str, arguments: Mapping[str, Any]) -> None:
        super().__init__(classes="entry tool", markup=False)
        self._name = name
        self._arguments = dict(arguments)
        self._result = ""
        self._ok = True

    def on_mount(self) -> None:
        self._refresh()

    def finish(self, *, ok: bool, preview: str) -> None:
        self._ok, self._result = ok, preview
        self._refresh()

    def _refresh(self) -> None:
        self.update(tool_render(self._name, self._arguments, result=self._result, ok=self._ok))


class PlatformNote(Static):
    """What the platform did, as a full-width rule."""

    def __init__(self, text: str) -> None:
        super().__init__(classes="entry platform", markup=False)
        self._text = text

    def on_mount(self) -> None:
        self.update(platform_rule(self._text))


class Notice(Static):
    """A warning or an error: a tinted, full-width box.

    Loud on purpose. "No API key" printed dim among tool output is a message people
    read after twenty minutes of wondering why nothing happens.
    """

    def __init__(self, text: str, *, level: str = "error") -> None:
        super().__init__(classes=f"entry notice {level}", markup=False)
        self._text = text

    def on_mount(self) -> None:
        self.update(Text(self._text))


class AgentRow(Horizontal):
    """A sub-agent's entry: its name in the gutter, its content indented."""

    def __init__(self, agent: str, content: Widget) -> None:
        super().__init__(classes="entry agent-row")
        self._agent = agent
        self.content = content

    def compose(self) -> Any:
        label = Static(Text(self._agent, style=f"bold {agent_colour(self._agent)}"), markup=False)
        label.add_class("agent-name")
        label.styles.width = len(self._agent) + 1
        yield label
        # The gutter is fixed, so the content takes what is left. Without this the
        # content keeps its full width and the right-hand border is clipped off.
        self.content.styles.width = "1fr"
        yield self.content


class Transcript(VerticalScroll):
    """The conversation. Entries are widgets; a sub-agent's are wrapped in a gutter."""

    def add(self, entry: Widget, *, agent: str = "") -> Widget:
        """Mount one entry and scroll to it. Returns the entry, not the wrapper, so a
        streaming caller keeps a handle on the thing it appends to."""
        self.mount(AgentRow(agent, entry) if agent else entry)
        self.scroll_end(animate=False)
        return entry

    def clear(self) -> None:
        self.remove_children()

    @property
    def scrolls(self) -> bool:
        """Whether there is more transcript than screen — what hides the banner."""
        return self.max_scroll_y > 0


__all__ = [
    "AGENT_COLOURS",
    "ARGUMENT_CHARS",
    "THINKING_LINES",
    "TOOL_RESULT_LINES",
    "AgentRow",
    "Message",
    "Notice",
    "PlatformNote",
    "Thinking",
    "ToolCall",
    "Transcript",
    "agent_colour",
    "format_arguments",
    "platform_rule",
    "thinking_tail",
    "tool_render",
]
