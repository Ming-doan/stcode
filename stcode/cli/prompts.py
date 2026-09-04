"""
The two modal screens the daemon can ask for: approve this, and answer this.

Both are the client half of a request–response pair correlated by `execution_id`
(CLAUDE.md §8 point 1). The screen returns a value; the caller sends it back under the
id it came with. Neither screen knows the id — that is the chat screen's bookkeeping,
and a modal that knew about correlation would be a modal that had to be told about the
socket.

Approval defaults to **deny**: Enter on a dialog you have not read should not be how a
`rm -rf` gets run. The Escape key does the same thing, for the same reason.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from stcode.cli import labels

_DIALOG_CSS = """
    ApprovalScreen, QuestionScreen {
        align: center middle;
        background: $background 60%;
    }

    #dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #dialog-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    #dialog-body {
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }

    #dialog-buttons {
        height: auto;
        align-horizontal: right;
    }

    #dialog-buttons Button {
        margin-left: 1;
    }
"""


class ApprovalScreen(ModalScreen[bool]):
    """Ask the human to approve one tool call. Returns True only on an explicit yes."""

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "deny", "Deny"),
        Binding("y", "approve", "Approve"),
        Binding("n", "deny", "Deny"),
    ]

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__()
        self._request = request

    def compose(self) -> ComposeResult:
        summary = labels.approval_summary(
            str(self._request.get("tool", "")),
            str(self._request.get("permission", "")),
            dict(self._request.get("arguments", {})),
        )
        with Vertical(id="dialog"):
            yield Label(labels.APPROVAL_TITLE, id="dialog-title")
            # `markup=False`: this is a shell command or a file path the model wrote,
            # and a stray `[` in it must not be parsed as a style tag.
            yield Static(summary, id="dialog-body", markup=False)
            with Horizontal(id="dialog-buttons"):
                yield Button(labels.APPROVAL_NO, variant="default", id="deny")
                yield Button(labels.APPROVAL_YES, variant="warning", id="approve")

    def on_mount(self) -> None:
        # Focus lands on Deny, so the reflexive Enter is the safe answer.
        self.query_one("#deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class QuestionScreen(ModalScreen[str]):
    """Put `ask_user_question` to the human. Returns the answer text, empty if dismissed.

    Options are shown as buttons *and* the free-text field stays open, because the tool's
    own docstring tells the model the user can always answer something else.
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "skip", "Skip")]

    def __init__(self, question: dict[str, Any]) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        header = str(self._question.get("header", "")) or labels.QUESTION_TITLE
        with Vertical(id="dialog"):
            yield Label(header, id="dialog-title")
            yield Static(str(self._question.get("question", "")), id="dialog-body", markup=False)
            for index, option in enumerate(self._question.get("options", [])):
                yield Button(str(option), id=f"option-{index}")
            yield Input(placeholder=labels.QUESTION_PLACEHOLDER, id="answer")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.label))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_skip(self) -> None:
        self.dismiss("")


__all__ = ["ApprovalScreen", "QuestionScreen"]
