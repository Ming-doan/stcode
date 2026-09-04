"""
Connect screen — point this client at a daemon.

`--daemonless` is the mode this exists for: the agent runs somewhere else, usually in a
container, and this terminal is only a window onto it. There is nothing to start here,
so a failed connection is a question — *which daemon?* — rather than an error.

It edits `[daemon]` and nothing else. Transport is the only field that changes what the
other fields mean, so switching it swaps which pair is shown rather than presenting four
inputs of which two are always ignored.

Like `SettingsScreen`, it never writes to disk: it dismisses with a `DaemonConfig` and
lets the app decide whether that is worth persisting.
"""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from stcode.cli import labels
from stcode.core.configs import DaemonConfig


class ConnectScreen(ModalScreen[DaemonConfig | None]):
    """Ask where the daemon is."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    CSS = """
    ConnectScreen {
        align: center middle;
    }

    #connect-dialog {
        width: 72;
        max-width: 96%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #connect-title {
        text-style: bold;
        color: $accent;
    }

    #connect-intro {
        color: $text-muted;
        margin-bottom: 1;
    }

    #connect-dialog Input, #connect-dialog Select, #connect-dialog SelectCurrent {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
        margin-bottom: 1;
    }

    #connect-dialog Input:focus, #connect-dialog Select:focus SelectCurrent {
        background: $primary 30%;
    }

    .field-label {
        text-style: bold;
        color: $accent;
    }

    #connect-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }

    #connect-buttons Button {
        margin-left: 2;
    }
    """

    def __init__(self, settings: DaemonConfig, *, reason: str = "") -> None:
        super().__init__()
        self._settings = settings
        self._reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-dialog"):
            yield Static(labels.CONNECT_TITLE, id="connect-title")
            yield Static(self._reason or labels.CONNECT_INTRO, id="connect-intro")

            yield Label(labels.FIELD_TRANSPORT, classes="field-label")
            yield Select(
                labels.transport_options(),
                value=self._settings.transport,
                allow_blank=False,
                id="transport",
            )

            yield Label(labels.FIELD_SOCKET, classes="field-label", id="socket-label")
            yield Input(value=self._settings.socket, id="socket")

            yield Label(labels.FIELD_HOST, classes="field-label", id="host-label")
            yield Input(
                value=self._settings.host, placeholder=labels.HOST_PLACEHOLDER, id="host"
            )

            yield Label(labels.FIELD_PORT, classes="field-label", id="port-label")
            yield Input(value=str(self._settings.port), id="port")

            with Horizontal(id="connect-buttons"):
                yield Button(labels.BUTTON_CANCEL, id="cancel")
                yield Button(labels.BUTTON_CONNECT, variant="primary", id="connect")

    def on_mount(self) -> None:
        self._show_fields_for(self._settings.transport)
        self.query_one("#connect", Button).focus()

    def _show_fields_for(self, transport: str) -> None:
        """Show only the pair the chosen transport actually uses."""
        unix = transport == "unix"
        for widget_id in ("#socket-label", "#socket"):
            self.query_one(widget_id).display = unix
        for widget_id in ("#host-label", "#host", "#port-label", "#port"):
            self.query_one(widget_id).display = not unix

    @on(Select.Changed, "#transport")
    def _transport_changed(self, event: Select.Changed) -> None:
        self._show_fields_for(str(event.value))

    @on(Input.Submitted)
    def _submit_on_enter(self) -> None:
        self._connect()

    @on(Button.Pressed, "#connect")
    def _connect_pressed(self) -> None:
        self._connect()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _connect(self) -> None:
        transport = str(self.query_one("#transport", Select).value)
        port_text = self.query_one("#port", Input).value.strip()
        try:
            port = int(port_text) if port_text else self._settings.port
        except ValueError:
            # A typo in the port is worth one sentence, not a dismissed dialog that
            # silently connects somewhere else.
            self.query_one("#connect-intro", Static).update(labels.bad_port(port_text))
            return

        self.dismiss(
            DaemonConfig(
                transport=transport,  # type: ignore[arg-type]
                socket=self.query_one("#socket", Input).value.strip() or self._settings.socket,
                host=self.query_one("#host", Input).value.strip() or self._settings.host,
                port=port,
            )
        )


__all__ = ["ConnectScreen"]
