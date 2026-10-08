from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from stcode.cli import labels


class TrustScreen(ModalScreen[bool]):
    """Ask whether the agent may work in this folder. Cancel means leave.

    No third "read-only for a bit" answer: that is `--mode plan` wearing a disguise,
    and a wall with a side door is a wall people learn to walk around.
    """

    CSS_PATH = "dialog.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, path: object) -> None:
        super().__init__()
        self._path = str(path)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(labels.TRUST_TITLE, id="dialog-title")
            body = Text()
            body.append(self._path + "\n\n", style="bold")
            body.append(labels.TRUST_BODY)
            yield Static(body, id="dialog-body", markup=False)
            with Horizontal(id="dialog-buttons"):
                yield Button(labels.TRUST_NO, id="cancel")
                yield Button(labels.TRUST_YES, variant="primary", id="trust")

    def on_mount(self) -> None:
        # Focus lands on Cancel: the reflexive Enter must not be how a folder gets
        # trusted, for the same reason approval defaults to deny.
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "trust")

    def action_cancel(self) -> None:
        self.dismiss(False)
