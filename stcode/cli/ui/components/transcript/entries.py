"""Transcript entry widgets; update in place as frames arrive."""

from __future__ import annotations
from typing import Any, Mapping
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static
from stcode.cli.ui.theme import brand_text
from stcode.cli.ui.components.transcript.formatting import (
    PROGRESS_LINES,
    agent_colour,
    thinking_tail,
    tool_render,
    shell_render,
    platform_rule,
)


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
            text.append(
                "you  ", style=f"bold {brand_text(self.app.current_theme.dark)}"
            )
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
        quoted = "\n".join(
            f"│ {line}" for line in thinking_tail(self.body).splitlines()
        )
        self._text.update(Text(quoted, style="italic dim"))
        self.scroll_end(animate=False)


class Progress(VerticalScroll):
    """A long-running tool's output while it is still running — `repl`'s cell printing,
    `bash`'s description, a web fetch saying which URL it is on.

    Plain dim text, in a scroller that shows the last few lines. Deliberately *not* a
    platform rule: a rule is one line saying what the platform did, and a REPL cell that
    prints forty lines of an MCP result is neither one line nor the platform. Rendering
    it as one produced forty dashed rules with a word centred in each.

    It is also not the model talking, so it stays dim and unattributed, and it is never
    written to the session — `Progress` is advisory, and the tool's own result is the
    record.
    """

    def __init__(self, body: str = "") -> None:
        super().__init__(classes="entry progress")
        self.body = body
        self._text = Static(markup=False)

    def compose(self) -> Any:
        yield self._text

    def on_mount(self) -> None:
        self._refresh()

    def append(self, text: str) -> None:
        # One frame per line, so the lines have to be rejoined here rather than run on.
        self.body = f"{self.body}\n{text}" if self.body else text
        self._refresh()

    def _refresh(self) -> None:
        self._text.update(Text(thinking_tail(self.body, PROGRESS_LINES), style="dim"))
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
        self._finished = False

    def on_mount(self) -> None:
        self._refresh()

    def finish(self, *, ok: bool, preview: str) -> None:
        self._ok, self._result, self._finished = ok, preview, True
        self._refresh()

    def _refresh(self) -> None:
        self.update(
            tool_render(
                self._name,
                self._arguments,
                result=self._result,
                ok=self._ok,
                finished=self._finished,
                colour=self._colour(),
            )
        )

    def _colour(self) -> str:
        return brand_text(self.app.current_theme.dark)


class ShellOutput(VerticalScroll):
    """A `!` command and its output, marked by a rule down the left.

    A rule rather than a box, because this is the one thing on the screen that is
    neither the model's nor the platform's: **you** ran it, and none of it is in the
    session. It should not look like anything the agent did.
    """

    def __init__(self, command: str) -> None:
        super().__init__(classes="entry shell")
        self._text = Static(markup=False)
        self._command = command
        self._output = ""
        self._exit_code = 0

    def compose(self) -> Any:
        yield self._text

    def on_mount(self) -> None:
        self._refresh()

    def finish(self, output: str, exit_code: int) -> None:
        self._output, self._exit_code = output, exit_code
        self._refresh()

    def _refresh(self) -> None:
        self._text.update(
            shell_render(self._command, self._output, exit_code=self._exit_code)
        )


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
        label = Static(
            Text(self._agent, style=f"bold {agent_colour(self._agent)}"), markup=False
        )
        label.add_class("agent-name")
        label.styles.width = len(self._agent) + 1
        yield label
        # The gutter is fixed, so the content takes what is left. Without this the
        # content keeps its full width and the right-hand border is clipped off.
        self.content.styles.width = "1fr"
        yield self.content
