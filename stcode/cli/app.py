"""
Chat screen — the main stcode UI.

Layout, top to bottom: ASCII wordmark, transcript, status bar (model / provider /
approval mode), prompt input, key hints.

Slash commands are handled here rather than by the agent: `/model` opens the settings
screen, `/mode` cycles the approval mode. Anything else is a prompt.

Scope note: the reply path streams straight through `LLMGateway` — one flat
user/assistant exchange, no REPL, no sub-agents, no tools. That is a deliberate
placeholder so the UI is exercisable end-to-end; when the RLM loop lands it replaces
`_stream_reply` and nothing else on this screen needs to change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Footer, Input, Static

from stcode.cli import labels
from stcode.cli.banner import banner_for_width
from stcode.cli.settings import SettingsScreen
from stcode.core.harness.approvals import ApprovalMode, next_approval_mode, parse_approval_mode
from stcode.core.configs import (
    GatewayConfig,
    config_exists,
    default_config_path,
    load_config,
    save_config,
)
from stcode.core.providers import LLMGateway, ProviderConfig, resolve_secret
from stcode.core.providers.types import Message, TextDelta


class Banner(Static):
    """The wordmark, swapped for a compact one when the terminal gets narrow."""

    def on_mount(self) -> None:
        self._render_for(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._render_for(event.size.width)

    def _render_for(self, width: int) -> None:
        self.update(banner_for_width(width))


class ChatMessage(Static):
    """One transcript entry. Body text is never parsed as markup — it's model or user
    output, and a stray `[` shouldn't blow up the render."""

    def __init__(self, role: str, body: str = "") -> None:
        super().__init__(classes=f"message {role}")
        self._role = role
        self.body = body

    def on_mount(self) -> None:
        self._refresh_body()

    def set_body(self, body: str) -> None:
        self.body = body
        self._refresh_body()

    def _refresh_body(self) -> None:
        prefix, style = labels.ROLE_PREFIX.get(self._role, labels.ROLE_PREFIX["notice"])
        gutter = f"{prefix}  "
        text = Text()
        text.append(gutter, style=style)
        # Hang subsequent lines under the first so multi-line output (/help, tracebacks)
        # stays in one visual column.
        text.append(self.body.replace("\n", "\n" + " " * len(gutter)))
        self.update(text)


class StcodeApp(App[None]):
    """stcode's terminal UI."""

    TITLE = "stcode"

    CSS = """
    Screen {
        background: $background;
    }

    #banner-area {
        height: auto;
        padding: 1 2 0 2;
    }

    #banner {
        color: $accent;
        text-align: center;
        height: auto;
    }

    #tagline {
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    #transcript {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }

    .message {
        margin-bottom: 1;
    }

    .message.user {
        color: $text;
    }

    .message.assistant {
        color: $text;
    }

    .message.notice {
        color: $text-muted;
    }

    #status-bar {
        height: 1;
        padding: 0 2;
        background: $panel;
        color: $text-muted;
    }

    #prompt {
        margin: 0 2;
        border: round $primary;
    }

    #prompt:focus {
        border: round $accent;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f2", "open_settings", "Settings"),
        # Screen-level focus navigation owns shift+tab by default, so this has to be a
        # priority binding to reach us at all.
        Binding("shift+tab", "cycle_mode", "Mode", priority=True),
        Binding("ctrl+l", "clear_transcript", "Clear"),
        Binding("escape", "cancel_stream", "Stop"),
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._config_path = config_path or default_config_path()
        self._first_run = not config_exists(self._config_path)
        self.config = GatewayConfig() if self._first_run else load_config(self._config_path)
        self._gateway: LLMGateway | None = None
        self._history: list[Message] = []

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="banner-area"):
            yield Banner("", id="banner", markup=False)
            yield Static(labels.TAGLINE, id="tagline")
        yield VerticalScroll(id="transcript")
        yield Static("", id="status-bar")
        yield Input(placeholder=labels.PROMPT_PLACEHOLDER, id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_status()
        self.query_one("#prompt", Input).focus()
        if self._first_run:
            self._open_settings(first_run=True)
        else:
            self._notice(labels.config_location(self._config_path))
            if not self.config.defaults.model:
                self._notice(labels.NO_MODEL_NOTICE)

    # ---------------------------------------------------------------- settings

    def action_open_settings(self) -> None:
        self._open_settings(first_run=False)

    def _open_settings(self, *, first_run: bool) -> None:
        def saved(config: GatewayConfig | None) -> None:
            if config is None:
                if first_run:
                    self._notice(labels.SETUP_SKIPPED)
                return
            self.config = config
            self._gateway = None  # credentials may have changed; rebuild on next send
            path = save_config(self.config, self._config_path)
            self._first_run = False
            self._refresh_status()
            self._notice(labels.config_saved(path))
            if not self.config.defaults.model:
                self._notice(labels.NO_MODEL_NOTICE)

        self.push_screen(SettingsScreen(self.config, first_run=first_run), saved)

    # -------------------------------------------------------------------- mode

    def action_cycle_mode(self) -> None:
        self._set_mode(next_approval_mode(self.config.defaults.approval_mode))

    def _set_mode(self, mode: ApprovalMode) -> None:
        self.config.defaults.approval_mode = mode
        self._refresh_status()
        self._notice(labels.mode_changed(mode))
        # Only persist once there's a config file; skipping setup shouldn't create one.
        if config_exists(self._config_path):
            save_config(self.config, self._config_path)

    # ------------------------------------------------------------- status line

    def _refresh_status(self) -> None:
        defaults = self.config.defaults
        mode = defaults.approval_mode
        text = Text()
        text.append("model ", style="dim")
        if defaults.model:
            text.append(defaults.model, style="bold")
        else:
            text.append(labels.STATUS_NO_MODEL, style="bold yellow")
        text.append("   provider ", style="dim")
        text.append(defaults.provider)
        text.append("   mode ", style="dim")
        text.append(mode, style=f"bold {labels.APPROVAL_MODE_COLOR.get(mode, 'white')}")
        if not self._has_credential():
            text.append(f"   {labels.STATUS_NO_KEY}", style="bold yellow")
        self.query_one("#status-bar", Static).update(text)

    def _has_credential(self) -> bool:
        provider = self.config.providers.get(self.config.defaults.provider, ProviderConfig())
        return bool(resolve_secret(provider.api_key_env, provider.api_key))

    # ------------------------------------------------------------------ output

    def _add_message(self, role: str, body: str) -> ChatMessage:
        widget = ChatMessage(role, body)
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(widget)
        transcript.scroll_end(animate=False)
        return widget

    def _notice(self, body: str) -> None:
        self._add_message("notice", body)

    def _error(self, body: str) -> None:
        self._add_message("error", body)

    def action_clear_transcript(self) -> None:
        self.query_one("#transcript", VerticalScroll).remove_children()
        self._history.clear()

    # ---------------------------------------------------------------- commands

    @on(Input.Submitted, "#prompt")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._run_command(text)
        else:
            self._send(text)

    def _run_command(self, raw: str) -> None:
        name, _, argument = raw[1:].partition(" ")
        name = name.lower()
        argument = argument.strip()

        if name in ("quit", "exit", "q"):
            self.exit()
        elif name == "model":
            self.action_open_settings()
        elif name == "mode":
            if not argument:
                self.action_cycle_mode()
                return
            mode = parse_approval_mode(argument)
            if mode is None:
                self._error(labels.unknown_mode(argument))
            else:
                self._set_mode(mode)
        elif name == "clear":
            self.action_clear_transcript()
        elif name == "help":
            self._notice(labels.COMMAND_HELP)
        else:
            self._error(labels.unknown_command(name))

    # ------------------------------------------------------------------- reply

    def _send(self, text: str) -> None:
        defaults = self.config.defaults
        if not defaults.model:
            self._add_message("user", text)
            self._error(labels.NO_MODEL_ERROR)
            return
        if not self._has_credential():
            self._add_message("user", text)
            self._error(labels.no_key_error(defaults.provider))
            return

        self._add_message("user", text)
        self._history.append(Message(role="user", content=text))
        self._stream_reply()

    def action_cancel_stream(self) -> None:
        if self.workers:
            self.workers.cancel_group(self, "chat")

    @work(exclusive=True, group="chat")
    async def _stream_reply(self) -> None:
        if self._gateway is None:
            self._gateway = LLMGateway(
                providers=self.config.providers,
                routing=self.config.routing,
                retry=self.config.retry,
            )

        defaults = self.config.defaults
        bubble = self._add_message("assistant", "…")
        transcript = self.query_one("#transcript", VerticalScroll)
        chunks: list[str] = []

        try:
            async for event in self._gateway.stream(
                self._history,
                provider=defaults.provider,
                model=defaults.model,
            ):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                    bubble.set_body("".join(chunks))
                    transcript.scroll_end(animate=False)
        except asyncio.CancelledError:
            # Stopped by esc or by a newer prompt. Keep whatever streamed, but leave
            # history strictly alternating — an unpaired user turn breaks the next call.
            self._settle(bubble, "".join(chunks), suffix=labels.STREAM_STOPPED_SUFFIX)
            raise
        except Exception as exc:  # noqa: BLE001 — surface any provider failure verbatim
            bubble.remove()
            self._error(labels.stream_failed(exc))
            self._history.pop()
            return

        self._settle(bubble, "".join(chunks))

    def _settle(self, bubble: ChatMessage, reply: str, suffix: str = "") -> None:
        """Close out a reply: an empty one is dropped along with the prompt that caused
        it, rather than recorded as an empty assistant turn."""
        if not reply:
            bubble.remove()
            self._history.pop()
            self._notice(labels.STREAM_STOPPED if suffix else labels.STREAM_EMPTY)
            return
        bubble.set_body(reply + suffix)
        self._history.append(Message(role="assistant", content=reply))


def run(config_path: Path | None = None) -> None:
    StcodeApp(config_path=config_path).run()
