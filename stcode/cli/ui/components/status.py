"""Session status and its presentation-only spinner."""

from __future__ import annotations
import contextlib
from typing import Callable
from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static
from stcode.cli import labels
from stcode.cli.logic.state import ClientState
from stcode.cli.ui.theme import PRIMARY

SPINNER_INTERVAL = 0.1


class Status(Static):
    DEFAULT_CSS = """
    Status { height: 1; color: $text-muted; }
    """

    def __init__(
        self, state: ClientState, *, daemonless: bool, address: Callable[[], str]
    ) -> None:
        super().__init__("", id="status")
        self.state = state
        self.daemonless = daemonless
        self.address = address
        self.working = False
        self._spinner = 0
        self._spinner_timer: Timer | None = None

    def on_mount(self) -> None:
        self.refresh_display()

    def on_unmount(self) -> None:
        self.stop_working()

    def refresh_display(self) -> None:
        effective, config = self.state.effective, self.state.config
        mode = effective.get("mode") or str(config.get("approval_mode", ""))
        model = effective.get("model") or str(config.get("model", ""))
        provider = self.state.provider

        text = Text()
        if self.working:
            frame = labels.SPINNER_FRAMES[self._spinner % len(labels.SPINNER_FRAMES)]
            text.append(labels.working_status(frame), style=f"bold {PRIMARY}")
        text.append("model ", style="dim")
        text.append(model or labels.STATUS_NO_MODEL, style="bold" if model else "dim")
        text.append("   provider ", style="dim")
        text.append(provider)
        if mode:
            text.append("   mode ", style="dim")
            text.append(
                mode, style=f"bold {labels.APPROVAL_MODE_COLOR.get(mode, 'white')}"
            )  # type: ignore[call-overload]
        effort = self.state.effort
        if effort:
            text.append("   effort ", style="dim")
            text.append(effort)
        if self.daemonless:
            # The one shape where "which agent am I talking to" is a live question.
            text.append("   daemon ", style="dim")
            text.append(self.address())
        with contextlib.suppress(Exception):
            self.update(text)

    def start_working(self) -> None:
        """Spin, from the moment the message is queued.

        The first token can be seconds away — a cold local model, a long system prompt,
        a retry — and until it arrives the screen is identical to one where nothing
        happened. A spinner in the status line is the difference between "it is
        thinking" and "did that send?", which is the only question anybody has in that
        gap.
        """
        if self.working:
            return
        self.working = True
        self._spinner = 0
        self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self._tick_spinner)
        self.refresh_display()

    def _tick_spinner(self) -> None:
        self._spinner += 1
        self.refresh_display()

    def stop_working(self) -> None:
        if not self.working:
            return
        self.working = False
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.refresh_display()
