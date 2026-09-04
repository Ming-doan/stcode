"""
Settings screen — the first-run wizard and the `/model` config page.

One screen serves both: on first run it's the "pick a provider and paste a key" prompt
that can be skipped, and afterwards it's where `/model` sends you. Keeping it single
means the two can't drift apart.

It never writes to disk. It dismisses with a new `GatewayConfig` (or `None` if the user
backed out) and lets the app own persistence — the screen stays a pure editor.
"""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from stcode.cli import labels
from stcode.core.configs import GatewayConfig, apply_provider_settings
from stcode.core.providers import PROVIDERS, ProviderConfig, key_env_for


class SettingsScreen(ModalScreen[GatewayConfig | None]):
    """Edit the default provider, credentials, and model."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    CSS = """
    SettingsScreen {
        align: center middle;
    }

    /* Fixed height with the fields in their own scroller, so the buttons stay on
       screen on a short terminal instead of sliding below the fold. */
    #settings-dialog {
        width: 72;
        max-width: 96%;
        height: 90%;
        max-height: 32;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }

    #settings-body {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    #settings-title {
        text-style: bold;
        color: $accent;
    }

    #settings-intro {
        color: $text-muted;
        margin-bottom: 1;
    }

    /* Single-row fields: bordered inputs are prettier but cost 3 rows each, which
       pushes Model and Base URL off a short terminal. Focus reads off the tinted
       background instead of a border. Select wraps its value in a SelectCurrent that
       carries its own border, so that has to be flattened too or the field renders
       as an empty box. */
    #settings-body Input, #settings-body Select, #settings-body SelectCurrent {
        height: 1;
        border: none;
        padding: 0 1;
        background: $boost;
    }

    #settings-body Input:focus, #settings-body Select:focus SelectCurrent {
        background: $primary 30%;
    }

    #settings-body Input, #settings-body Select {
        margin-bottom: 1;
    }

    .field-label {
        text-style: bold;
        color: $accent;
    }

    .field-hint {
        color: $text-muted;
    }

    #settings-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }

    #settings-buttons Button {
        margin-left: 2;
    }
    """

    def __init__(self, config: GatewayConfig, *, first_run: bool = False) -> None:
        super().__init__()
        self._config = config
        self._first_run = first_run

    def compose(self) -> ComposeResult:
        provider = self._current_provider()
        stored = self._config.providers.get(provider, ProviderConfig())

        with Vertical(id="settings-dialog"):
            yield Static(
                labels.SETTINGS_TITLE_FIRST_RUN if self._first_run else labels.SETTINGS_TITLE,
                id="settings-title",
            )
            yield Static(
                labels.SETTINGS_INTRO_FIRST_RUN if self._first_run else labels.SETTINGS_INTRO,
                id="settings-intro",
            )

            with VerticalScroll(id="settings-body"):
                yield Label(labels.FIELD_PROVIDER, classes="field-label")
                yield Select(
                    labels.provider_options(), value=provider, allow_blank=False, id="provider"
                )

                yield Label(labels.FIELD_API_KEY, classes="field-label")
                yield Input(
                    password=True,
                    placeholder=labels.key_placeholder(provider, stored),
                    id="api-key",
                )
                yield Static(
                    labels.key_hint(provider, stored), id="key-hint", classes="field-hint"
                )

                yield Label(labels.FIELD_BASE_URL, classes="field-label")
                yield Input(
                    value=stored.base_url or "",
                    placeholder=labels.BASE_URL_PLACEHOLDER,
                    id="base-url",
                )

                yield Label(labels.FIELD_MODEL, classes="field-label")
                yield Input(
                    value=self._config.defaults.model,
                    placeholder=labels.model_placeholder(provider),
                    id="model",
                )

            with Horizontal(id="settings-buttons"):
                yield Button(
                    labels.BUTTON_SKIP if self._first_run else labels.BUTTON_CANCEL, id="cancel"
                )
                yield Button(labels.BUTTON_SAVE, variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#api-key", Input).focus()

    def _current_provider(self) -> str:
        provider = self._config.defaults.provider
        return provider if provider in PROVIDERS else "openai"

    @on(Select.Changed, "#provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        provider = str(event.value)
        stored = self._config.providers.get(provider, ProviderConfig())

        key_input = self.query_one("#api-key", Input)
        key_input.value = ""
        key_input.placeholder = labels.key_placeholder(provider, stored)
        self.query_one("#key-hint", Static).update(labels.key_hint(provider, stored))

        self.query_one("#base-url", Input).value = stored.base_url or ""
        # The chosen model belongs to the old provider — clear it rather than send
        # e.g. a gpt-* name to Anthropic.
        model_input = self.query_one("#model", Input)
        model_input.value = ""
        model_input.placeholder = labels.model_placeholder(provider)

    @on(Input.Submitted)
    def _submit_on_enter(self) -> None:
        self._save()

    @on(Button.Pressed, "#save")
    def _save_pressed(self) -> None:
        self._save()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        provider = str(self.query_one("#provider", Select).value)
        key = self.query_one("#api-key", Input).value.strip()
        base_url = self.query_one("#base-url", Input).value.strip()
        model = self.query_one("#model", Input).value.strip()
        stored = self._config.providers.get(provider, ProviderConfig())

        if key:
            # A literal key the user just typed should win outright — clear any env-var
            # pointer so a stale exported variable can't silently shadow it.
            api_key, api_key_env = key, ""
        elif not stored.api_key and not stored.api_key_env:
            # Nothing typed and nothing stored: leave the conventional env var wired up
            # so exporting it later just works.
            api_key, api_key_env = None, key_env_for(provider)
        else:
            api_key, api_key_env = None, None  # keep what's already there

        self.dismiss(
            apply_provider_settings(
                self._config,
                provider=provider,
                api_key=api_key,
                api_key_env=api_key_env,
                base_url=base_url,
                model=model,
            )
        )
