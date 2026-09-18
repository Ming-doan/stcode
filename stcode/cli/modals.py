"""
The two things that are still modals: trusting a folder, and picking a session.

Everything else moved to a [card](cards.py) above the input, because a modal covers the
transcript you need in order to answer. These two are the exceptions, and for opposite
reasons:

* **Trust** is a wall, not a question about the conversation. There is nothing behind it
  worth reading yet, and Cancel ends the program.
* **Sessions** is a list of other conversations, so covering this one costs nothing.
"""

from __future__ import annotations

from typing import Any, ClassVar, Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, Tree
from textual.widgets.tree import TreeNode

from stcode.cli import labels

MODAL_CSS = """
    TrustScreen, SessionsScreen {
        align: center middle;
        background: $background 70%;
    }

    #dialog {
        width: 80;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }

    #dialog-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #dialog-body {
        height: auto;
        margin-bottom: 1;
    }

    #dialog-buttons {
        height: auto;
        align-horizontal: right;
    }

    #dialog-buttons Button {
        margin-left: 2;
    }

    #session-tree {
        height: auto;
        max-height: 16;
        margin-bottom: 1;
    }
"""


class TrustScreen(ModalScreen[bool]):
    """Ask whether the agent may work in this folder. Cancel means leave.

    No third "read-only for a bit" answer: that is `--mode plan` wearing a disguise,
    and a wall with a side door is a wall people learn to walk around.
    """

    CSS = MODAL_CSS

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


class SessionsScreen(ModalScreen[str]):
    """Pick an earlier session to resume. Returns its id, or "" if nothing was picked.

    A tree, because sub-agent sessions are rows in the same listing: `parent` and
    `agent_name` are already in every `meta` line, so the shape is a group-by and not
    an index.

    **Only a parent is selectable.** A sub-agent's session is a transcript to read, not
    a conversation to continue — there is no user on the other end of it, and it was
    built to answer exactly one question.
    """

    CSS = MODAL_CSS

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        super().__init__()
        self._rows = list(rows)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(labels.SESSIONS_TITLE, id="dialog-title")
            if not self._rows:
                yield Static(labels.SESSIONS_EMPTY, id="dialog-body", markup=False)
            else:
                yield Static(
                    Text(labels.SESSIONS_INTRO, style="dim"), id="dialog-body", markup=False
                )
                yield self._tree()
            with Horizontal(id="dialog-buttons"):
                yield Button(labels.BUTTON_CANCEL, id="cancel")

    def _tree(self) -> Tree[dict[str, Any]]:
        tree: Tree[dict[str, Any]] = Tree("sessions", id="session-tree")
        tree.show_root = False
        tree.guide_depth = 3

        children: dict[str, list[dict[str, Any]]] = {}
        for row in self._rows:
            parent = str(row.get("parent", ""))
            if parent:
                children.setdefault(parent, []).append(row)

        known = {str(row.get("id", "")) for row in self._rows}
        for row in self._rows:
            parent = str(row.get("parent", ""))
            # An orphan — its parent has been pruned — is shown at the top level rather
            # than hidden. A transcript you cannot find is a transcript you have lost.
            if parent and parent in known:
                continue
            node = tree.root.add(self._label(row), data=row, expand=True)
            for child in children.get(str(row.get("id", "")), []):
                node.add_leaf(self._child_label(child), data=child)
        return tree

    def _label(self, row: dict[str, Any]) -> Text:
        text = Text()
        text.append(labels.session_line(row), style="bold" if row.get("live") else "")
        summary = str(row.get("summary", ""))
        if summary:
            text.append(f"  {summary}", style="dim")
        return text

    def _child_label(self, row: dict[str, Any]) -> Text:
        return Text(labels.subsession_line(row), style="dim")

    def on_mount(self) -> None:
        if self._rows:
            self.query_one("#session-tree", Tree).focus()
        else:
            self.query_one("#cancel", Button).focus()

    @on(Tree.NodeSelected)
    def _selected(self, event: Tree.NodeSelected[dict[str, Any]]) -> None:
        node: TreeNode[dict[str, Any]] = event.node
        row = node.data or {}
        if row.get("parent"):
            return  # A sub-agent's session is readable, not resumable.
        self.dismiss(str(row.get("id", "")))

    @on(Button.Pressed, "#cancel")
    def _cancelled(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss("")


__all__ = ["MODAL_CSS", "SessionsScreen", "TrustScreen"]
