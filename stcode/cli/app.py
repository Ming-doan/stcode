"""
Chat screen — the main stcode UI, and a **client of the daemon**.

Top to bottom: wordmark, transcript, status bar, prompt input, key hints. Slash commands
are handled here, not by the agent; anything else is a prompt.

This screen owns no conversation. It opens a socket, sends `push`, and renders frames —
history, tools and approvals all live behind that socket, which is why closing the
window no longer stops the work.

**Connecting is find-or-start.** A daemon already listening is used as-is; otherwise one
starts in this process on the same address. The transport is real either way, so the
"attach to a container" path is the one exercised every day. `--daemonless` removes the
"or start" half and asks *which daemon?* instead — one branch, because the client is the
same client.

The two request–response pairs (`approval_request`, `question`) are answered by a modal
whose result goes back under the `execution_id` it arrived with. Deliberately not
awaited inline: parallel tool calls can raise two at once, and a pump blocked on a
dialog would stop rendering the tool still running behind it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Callable, ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Footer, Input, Static

from stcode.cli import labels
from stcode.cli.banner import banner_for_width
from stcode.cli.connect import ConnectScreen
from stcode.cli.prompts import ApprovalScreen, QuestionScreen
from stcode.cli.settings import SettingsScreen
from stcode.core.configs import (
    GatewayConfig,
    apply_cli_overrides,
    config_exists,
    default_config_path,
    load_config,
    save_config,
)
from stcode.core.daemon import Daemon, DaemonClient
from stcode.core.harness.approvals import ApprovalMode, next_approval_mode, parse_approval_mode
from stcode.core.providers import ProviderConfig, resolve_secret


class Banner(Static):
    """The wordmark, swapped for a compact one when the terminal gets narrow."""

    def on_mount(self) -> None:
        self._render_for(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._render_for(event.size.width)

    def _render_for(self, width: int) -> None:
        self.update(banner_for_width(width))


class ChatMessage(Static):
    """One transcript entry. Body text is never parsed as markup — it is model or user
    output, and a stray `[` should not blow up the render."""

    def __init__(self, role: str, body: str = "") -> None:
        super().__init__(classes=f"message {role}")
        self._role = role
        self.body = body

    def on_mount(self) -> None:
        self._refresh_body()

    def append(self, text: str) -> None:
        self.body += text
        self._refresh_body()

    def set_body(self, body: str) -> None:
        self.body = body
        self._refresh_body()

    def _refresh_body(self) -> None:
        prefix, style = labels.ROLE_PREFIX.get(self._role, labels.ROLE_PREFIX["notice"])
        gutter = f"{prefix}  "
        text = Text()
        text.append(gutter, style=style)
        # Hang later lines under the first, so multi-line output stays in one column.
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

    .message.thinking {
        color: $text-muted;
        text-style: italic;
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
        # Focus navigation owns shift+tab, so this must be a priority binding.
        Binding("shift+tab", "cycle_mode", "Mode", priority=True),
        Binding("ctrl+l", "clear_transcript", "Clear"),
        Binding("escape", "cancel_stream", "Stop"),
    ]

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        daemonless: bool = False,
        cwd: Path | None = None,
        resume: str = "",
        overrides: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._config_path = config_path or default_config_path()
        self._first_run = not config_exists(self._config_path)
        self.config = GatewayConfig() if self._first_run else load_config(self._config_path)
        # In-memory only: a flag must never become a stored default.
        self._overrides = overrides or {}
        apply_cli_overrides(self.config, **self._overrides)
        # `--daemonless`: never start an agent here. Starting a local one would run the
        # work on this machine instead — the opposite of what was asked for.
        self._daemonless = daemonless
        self._cwd = cwd or Path.cwd()
        self._resume = resume
        self._client: DaemonClient | None = None
        # Set only when this process started the daemon — the one case where quitting
        # should stop it.
        self._daemon: Daemon | None = None
        self._session_id = ""
        self._stream: ChatMessage | None = None
        self._stream_role = ""

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
            return
        self._notice(labels.config_location(self._config_path))
        if not self.config.defaults.model:
            self._notice(labels.NO_MODEL_NOTICE)
        self._connect()

    async def on_unmount(self) -> None:
        await self._close_connection()

    async def _close_connection(self) -> None:
        """Drop the connection, and stop the daemon only if we started it.

        Quitting the UI is a detach, and detaching from someone else's daemon must leave
        it running — that is the promise the whole layer exists for.
        """
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        if self._daemon is not None:
            with contextlib.suppress(Exception):
                await self._daemon.aclose()
            self._daemon = None

    # ------------------------------------------------------------------- daemon

    @work(exclusive=True, group="daemon")
    async def _connect(self, ask: bool = False) -> None:
        """Find or start a daemon, open a session, then render its frames forever.

        Exclusive, so calling it again after a settings change cancels the running pump
        first. The old connection closes here rather than there, because the worker that
        owned it is already cancelled by the time we run.
        """
        await self._close_connection()
        self._session_id = ""
        self._notice(labels.CONNECTING)
        try:
            client, embedded = (
                (await self._connect_elsewhere(), False) if ask else await self._open_client()
            )
        except Exception as exc:  # noqa: BLE001 — including AutonomyRefused, which is a
            # refusal the user has to read: `full-auto` on the host does not start.
            self._error(labels.daemon_failed(exc))
            return

        self._client = client
        self._notice(labels.daemon_connected(self._daemon_address(), embedded))
        try:
            if self._resume:
                # `attach` resumes from disk when the daemon is not holding it, so one
                # message covers both "join the running session" and "reopen the
                # transcript" without the client knowing which happened.
                info = await client.attach(self._resume)
            else:
                info = await client.create(
                    cwd=self._cwd, approval_mode=self.config.defaults.approval_mode
                )
        except Exception as exc:  # noqa: BLE001
            self._error(labels.daemon_failed(exc))
            return

        self._session_id = str(info.get("id", ""))
        self._notice(labels.session_started(self._session_id, info.get("cwd", "")))

        async for frame in client.events():
            self._render(frame)

    async def _open_client(self) -> tuple[DaemonClient, bool]:
        """Connect, or start a daemon here and connect to that.

        The `OSError` is the interesting case, not a failure: nothing listening is
        exactly how "no daemon yet" looks. What it means depends on the mode — start
        one, or ask where the right one is.
        """
        try:
            return await DaemonClient.connect(self.config), False
        except OSError:
            pass
        if self._daemonless:
            return await self._connect_elsewhere(), False
        self._notice(labels.DAEMON_STARTING)
        daemon = Daemon(self.config)
        await daemon.start()
        self._daemon = daemon
        return await DaemonClient.connect(self.config), True

    async def _connect_elsewhere(self) -> DaemonClient:
        """Ask for an address and keep trying until one answers or the user gives up.

        Also what `/connect` runs: moving this terminal to a different agent is the same
        act whether or not one runs here. A loop rather than one shot, because getting a
        container's host and port right first try is not the common case, and a typo
        should not drop the user back to an empty screen.
        """
        reason = labels.no_daemon_here(self._daemon_address())
        while True:
            settings = await self.push_screen_wait(
                ConnectScreen(self.config.daemon, reason=reason)
            )
            if settings is None:
                raise ConnectionError(labels.DAEMONLESS_CANCELLED)
            self.config.daemon = settings
            try:
                client = await DaemonClient.connect(self.config)
            except OSError:
                reason = labels.CONNECT_RETRY
                continue
            # The next run should start where this one ended up.
            if config_exists(self._config_path):
                save_config(self.config, self._config_path)
            return client

    def _daemon_address(self) -> str:
        settings = self.config.daemon
        if settings.transport == "unix":
            return settings.socket
        return f"{settings.host}:{settings.port}"

    # ---------------------------------------------------------------- rendering

    def _render(self, frame: dict[str, Any]) -> None:
        match frame.get("type"):
            case "text_delta":
                self._stream_into("assistant", str(frame.get("text", "")))
            case "reasoning_delta":
                self._stream_into("thinking", str(frame.get("text", "")))
            case "tool_started":
                self._notice(
                    labels.tool_started(str(frame.get("name", "")), dict(frame.get("arguments", {})))
                )
            case "tool_finished":
                self._notice(
                    labels.tool_finished(
                        str(frame.get("name", "")),
                        bool(frame.get("ok", True)),
                        str(frame.get("preview", "")),
                    )
                )
            case "turn_finished":
                self._end_stream()
                self._notice(labels.turn_usage(dict(frame.get("usage", {}))))
            case "agent_failed":
                self._end_stream()
                self._error(str(frame.get("message", "")))
            case "approval_request":
                self._ask_approval(frame)
            case "question":
                self._ask_question(frame)
            case "supervisor":
                self._end_stream()
                self._notice(labels.supervisor_nudge(str(frame.get("text", ""))))
            case "progress":
                self._notice(str(frame.get("text", "")))
            case "history":
                self._replay(frame)
            case "session":
                # The daemon is authoritative: a refused `set_mode` must not leave the
                # status bar showing a mode that is not in force, nor reach the config.
                mode = str(frame.get("approval_mode", ""))
                if mode:
                    self._adopt_mode(mode)  # type: ignore[arg-type]
                self._refresh_status()
            case "error":
                self._error(str(frame.get("message", "")))

    def _replay(self, frame: dict[str, Any]) -> None:
        """Render a re-attached session's transcript before its live stream arrives.

        Raw records, so this shows what happened rather than what the model was sent.
        """
        for record in frame.get("records", []):
            match record.get("type"):
                case "user":
                    self._add_message("user", str(record.get("content", "")))
                case "assistant" if record.get("content"):
                    self._add_message("assistant", str(record.get("content", "")))
                case "tool_call":
                    self._notice(
                        labels.tool_started(
                            str(record.get("name", "")), dict(record.get("arguments", {}))
                        )
                    )
                case "supervisor":
                    self._notice(labels.supervisor_nudge(str(record.get("content", ""))))
                case "inbox":
                    self._notice(
                        labels.inbox_message(
                            str(record.get("from", "")),
                            str(record.get("subject", "")),
                            [str(ref) for ref in record.get("refs", [])],
                        )
                    )
                case "error":
                    self._error(str(record.get("message", "")))

    def _stream_into(self, role: str, text: str) -> None:
        """Append a delta, opening a new bubble when the kind of delta changes.

        Deltas carry no boundaries, so the only signal a block ended is that a different
        kind of event arrived.
        """
        if self._stream is None or self._stream_role != role:
            self._stream = self._add_message(role, "")
            self._stream_role = role
        self._stream.append(text)
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    def _end_stream(self) -> None:
        if self._stream is not None and not self._stream.body.strip():
            self._stream.remove()
        self._stream = None
        self._stream_role = ""

    # ---------------------------------------------------------------- approvals

    def _ask_approval(self, frame: dict[str, Any]) -> None:
        self._ask(frame, ApprovalScreen, self._answer_approval, bool)

    def _ask_question(self, frame: dict[str, Any]) -> None:
        self._ask(frame, QuestionScreen, self._answer_question, lambda v: v or "")

    def _ask(
        self,
        frame: dict[str, Any],
        screen: type[Any],
        answer: Callable[[str, Any], None],
        coerce: Callable[[Any], Any],
    ) -> None:
        """Show a modal and send its result back under the id the request arrived with."""
        execution_id = str(frame.get("execution_id", ""))
        self._end_stream()
        self.push_screen(screen(frame), lambda value: answer(execution_id, coerce(value)))

    @work(group="answers")
    async def _answer_approval(self, execution_id: str, approved: bool) -> None:
        if self._client is not None:
            await self._client.approve(execution_id, approved)
        if not approved:
            self._notice(labels.APPROVAL_DENIED)

    @work(group="answers")
    async def _answer_question(self, execution_id: str, text: str) -> None:
        if self._client is not None:
            await self._client.answer(execution_id, text)

    # ---------------------------------------------------------------- settings

    def action_open_settings(self) -> None:
        self._open_settings(first_run=False)

    def _open_settings(self, *, first_run: bool) -> None:
        def saved(config: GatewayConfig | None) -> None:
            if config is None:
                if first_run:
                    self._notice(labels.SETUP_SKIPPED)
                return
            path = save_config(config, self._config_path)
            # Saved first, then flags back on top: what the user typed belongs in the
            # file, what they passed on the command line belongs only to this run.
            self.config = apply_cli_overrides(config, **self._overrides)
            self._first_run = False
            self._refresh_status()
            self._notice(labels.config_saved(path))
            if not self.config.defaults.model:
                self._notice(labels.NO_MODEL_NOTICE)
            # The daemon built its gateway from the old config, so new credentials
            # reach it only through a new connection.
            self._connect()

        self.push_screen(SettingsScreen(self.config, first_run=first_run), saved)

    # -------------------------------------------------------------------- mode

    def action_cycle_mode(self) -> None:
        self._set_mode(next_approval_mode(self.config.defaults.approval_mode))

    def _set_mode(self, mode: ApprovalMode) -> None:
        """Ask for a mode. The daemon decides whether it gets one.

        Nothing is announced or written while a session is attached: the daemon can
        refuse `full-auto` on the host, and announcing first would both lie and
        *persist* a mode it will refuse to start in next time. The `session` frame it
        sends back is the answer, and `_adopt_mode` is where a confirmed change lands.
        """
        if self._client is not None and self._session_id:
            self._push_mode(mode)
            return
        self.config.defaults.approval_mode = mode
        self._refresh_status()
        self._notice(labels.mode_changed(mode))
        self._persist()

    def _adopt_mode(self, mode: ApprovalMode) -> None:
        """Take the mode the daemon reports as the truth, and only then write it down."""
        if mode == self.config.defaults.approval_mode:
            return
        self.config.defaults.approval_mode = mode
        self._notice(labels.mode_changed(mode))
        self._persist()

    def _persist(self) -> None:
        """Only once a config file exists; skipping setup should not create one."""
        if config_exists(self._config_path):
            save_config(self.config, self._config_path)

    @work(group="answers")
    async def _push_mode(self, mode: ApprovalMode) -> None:
        """Tell the live session, so `/mode` means something before the next session."""
        if self._client is not None and self._session_id:
            await self._client.set_mode(mode)

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
        self._end_stream()
        self._add_message("notice", body)

    def _error(self, body: str) -> None:
        self._end_stream()
        self._add_message("error", body)

    def action_clear_transcript(self) -> None:
        """Clears the *view*. The session is append-only and is not touched."""
        self.query_one("#transcript", VerticalScroll).remove_children()
        self._stream = None
        self._stream_role = ""

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

        match name:
            case "quit" | "exit" | "q":
                self.exit()
            case "model":
                self.action_open_settings()
            case "mode" if not argument:
                self.action_cycle_mode()
            case "mode":
                mode = parse_approval_mode(argument)
                if mode is None:
                    self._error(labels.unknown_mode(argument))
                else:
                    self._set_mode(mode)
            case "connect":
                self._connect(ask=True)
            case "clear":
                self.action_clear_transcript()
            case "help":
                self._notice(labels.COMMAND_HELP)
            case _:
                self._error(labels.unknown_command(name))

    # ------------------------------------------------------------------- reply

    def _send(self, text: str) -> None:
        defaults = self.config.defaults
        self._add_message("user", text)
        if not defaults.model:
            self._error(labels.NO_MODEL_ERROR)
            return
        if not self._has_credential():
            self._error(labels.no_key_error(defaults.provider))
            return
        if self._client is None or not self._session_id:
            self._error(labels.CONNECTING)
            return
        self._push(text)

    @work(group="push")
    async def _push(self, text: str) -> None:
        """Queue the message. A push arriving mid-turn waits for the next one; it never
        splices into the turn in flight."""
        if self._client is not None:
            await self._client.push(text)

    def action_cancel_stream(self) -> None:
        """Esc stops the agent. `push` never does; this is the message that does."""
        self._interrupt()

    @work(group="answers")
    async def _interrupt(self) -> None:
        if self._client is not None and self._session_id:
            await self._client.interrupt()
            self._notice(labels.STREAM_STOPPED)


def run(
    config_path: Path | None = None,
    *,
    daemonless: bool = False,
    cwd: Path | None = None,
    resume: str = "",
    overrides: dict[str, Any] | None = None,
) -> None:
    StcodeApp(
        config_path=config_path,
        daemonless=daemonless,
        cwd=cwd,
        resume=resume,
        overrides=overrides,
    ).run()
