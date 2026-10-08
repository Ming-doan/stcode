"""
Settings screen — the first-run wizard and the `/model` page.

One screen for both, so they cannot drift apart. It shows the daemon's `config` frame
and dismisses with a `set_config` patch (or `None` if the user backed out); the app
sends it and the daemon writes its own file. Nothing here touches a disk.

Three fields: provider, key, model. Routing, base URLs and concurrency caps live in the
daemon's `config.toml`. The key field is disabled when the frame says keys are not
editable — a daemon reached over TCP accepts none.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from stcode.cli import labels


class SettingsScreen(ModalScreen[dict[str, Any] | None]):
    """Edit the daemon's default provider, its key, and its model."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    CSS_PATH = "settings.tcss"

    # Textual focuses this once the screen's children exist.
    AUTO_FOCUS = "#model"

    def __init__(self, config: dict[str, Any], *, first_run: bool = False) -> None:
        super().__init__()
        self._config = config
        self._first_run = first_run
        self._keys_editable = bool(config.get("keys_editable"))

    def compose(self) -> ComposeResult:
        provider = str(self._config.get("provider", ""))
        with Vertical(id="settings-dialog"):
            yield Static(
                labels.SETTINGS_TITLE_FIRST_RUN
                if self._first_run
                else labels.SETTINGS_TITLE,
                id="settings-title",
            )
            yield Static(
                labels.SETTINGS_INTRO_FIRST_RUN
                if self._first_run
                else labels.SETTINGS_INTRO,
                id="settings-intro",
            )
            with VerticalScroll(id="settings-body"):
                yield Label(labels.FIELD_PROVIDER, classes="field-label")
                options = labels.provider_options(self._config)
                yield Select(
                    options,
                    value=provider
                    if any(value == provider for _, value in options)
                    else Select.BLANK,
                    allow_blank=not options,
                    id="provider",
                )
                yield Label(labels.FIELD_API_KEY, classes="field-label")
                yield Input(
                    password=True, id="api-key", disabled=not self._keys_editable
                )
                yield Static("", id="key-hint", classes="field-hint")
                yield Label(labels.FIELD_MODEL, classes="field-label")
                yield Input(id="model")
            with Horizontal(id="settings-buttons"):
                yield Button(
                    labels.BUTTON_SKIP if self._first_run else labels.BUTTON_CANCEL,
                    id="cancel",
                )
                yield Button(labels.BUTTON_SAVE, variant="primary", id="save")

    def on_mount(self) -> None:
        self._show(str(self._config.get("provider", "")))

    def _entry(self, name: str) -> tuple[dict[str, Any], dict[str, str]]:
        """The configured entry for `name` (empty when it would be new), and its library."""
        entry = dict(self._config.get("providers", {}).get(name, {}))
        library = str(entry.get("provider") or name)
        return entry, dict(self._config.get("libraries", {}).get(library, {}))

    def _show(self, name: str) -> None:
        """Fill the key and model fields for one provider entry."""
        entry, library = self._entry(name)
        has_key = bool(entry.get("has_key"))
        key_input = self.query_one("#api-key", Input)
        key_input.value = ""
        key_input.placeholder = labels.key_placeholder(has_key)
        self.query_one("#key-hint", Static).update(
            labels.key_hint(
                library.get("key_env", ""),
                has_key=has_key,
                editable=self._keys_editable,
            )
        )
        model_input = self.query_one("#model", Input)
        model_input.value = str(entry.get("model", ""))
        model_input.placeholder = labels.model_placeholder(
            library.get("default_model", "")
        )

    @on(Select.Changed, "#provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self._show(str(event.value))

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        provider = self.query_one("#provider", Select).value
        if provider is Select.BLANK:
            return
        patch: dict[str, Any] = {
            "provider": str(provider),
            "model": self.query_one("#model", Input).value.strip(),
        }
        key = self.query_one("#api-key", Input).value.strip()
        if key:
            patch["api_key"] = key
        self.dismiss(patch)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["SettingsScreen"]
