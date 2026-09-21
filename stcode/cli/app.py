"""
Chat screen — the main stcode UI, and a **client of the daemon**.

Top to bottom: wordmark, transcript, a card when one is open, the input, and the status
line under it. Slash commands are handled here, not by the agent; anything else is a
prompt.

This screen owns no conversation. It opens a socket, sends `push`, and renders frames —
history, tools and approvals all live behind that socket, which is why closing the
window no longer stops the work.

**Connecting is find-or-start.** A daemon already listening is used as-is; otherwise one
starts in this process on the same address. The transport is real either way, so the
"attach to a container" path is the one exercised every day. `--daemonless` removes the
"or start" half and asks *which daemon?* instead — one branch, because the client is the
same client.

**Approval and questions are cards, not modals** ([cards.py](cards.py)): a modal covers
the transcript, which is the thing you need in order to answer. Two can arrive at once
from parallel tool calls, so they queue and the turn keeps streaming behind them.

What is deliberately *not* announced: the config path, and "Connecting…". A path nobody
asked for is noise on every start, and a connection that worked does not need a
sentence. `?` has the paths when you want them.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections import deque
from contextlib import aclosing
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.timer import Timer
from textual.widgets import Static

from stcode.cli import labels
from stcode.cli.banner import Banner
from stcode.cli.cards import Card, CardZone, filter_rows, token_trigger
from stcode.cli.connect import ConnectScreen
from stcode.cli.modals import SessionsScreen, TrustScreen
from stcode.cli.prefs import (
    ThemePreference,
    UiPrefs,
    clamp_shell_timeout,
    load_prefs,
    save_prefs,
    ui_path,
)
from stcode.cli.prompt import Prompt
from stcode.cli.settings import SettingsScreen
from stcode.cli.theme import PRIMARY, THEMES, TerminalMode, detect_terminal_mode, theme_name_for
from stcode.cli.transcript import (
    Message,
    Notice,
    PlatformNote,
    Progress,
    ShellOutput,
    Thinking,
    ToolCall,
    Transcript,
)
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

SPINNER_INTERVAL = 0.1
"""How often the working spinner advances. Ten frames a second reads as motion without
costing a redraw people can feel."""

FILE_LIMIT = 2000
"""How many paths `@` offers. Past this the list is not a list you read, and the filter
is doing the work anyway."""

STREAM_KINDS = ("assistant", "thinking", "progress")
"""The block kinds that arrive in pieces. One open block per kind per agent, and a
piece of one kind closes the others: the frames carry no boundaries, so a change of
kind is the only end-of-block signal there is."""

_OPEN_STREAM = {
    "assistant": lambda: Message("assistant"),
    "thinking": Thinking,
    "progress": Progress,
}


class StcodeApp(App[None]):
    """stcode's terminal UI."""

    TITLE = "stcode"

    ENABLE_COMMAND_PALETTE = False
    """The twelve commands in `/` are the whole surface. A second, fuzzy way to reach
    the same things is a second place for them to drift."""

    CSS = """
    Screen {
        background: $background;
    }

    #banner {
        height: auto;
        padding: 1 2 1 2;
        color: $primary;
        text-align: center;
    }

    #transcript {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }

    .entry {
        height: auto;
        margin-bottom: 1;
    }

    .entry.platform {
        margin-bottom: 1;
    }

    /* What you said, tinted. Without it a short prompt is three words of plain text in
       a column of plain text, and scrolling back to find where you asked something
       means reading rather than looking. */
    .entry.message.user {
        width: 1fr;
        padding: 0 1;
        background: $primary 12%;
    }

    /* A `!` command: a rule down the left and nothing else. It is neither the model's
       nor the platform's, and none of it is in the session. */
    .entry.shell {
        width: 1fr;
        padding: 0 1;
        border-left: thick $primary;
    }

    .entry.thinking, .entry.progress {
        max-height: 6;
        scrollbar-size-vertical: 1;
    }

    /* Full width and tinted: "no API key" printed dim among tool output is a message
       people read twenty minutes after they needed it. */
    .entry.notice {
        width: 1fr;
        padding: 0 1;
        color: $foreground;
    }

    .entry.notice.error {
        background: $error 25%;
    }

    .entry.notice.warning {
        background: $warning 25%;
    }

    .entry.agent-row {
        height: auto;
    }

    /* The row already carries the gap. Without this the entry inside it adds a second
       one and a sub-agent's output reads as twice as far apart as the main agent's. */
    .agent-row .entry {
        margin-bottom: 0;
    }

    .agent-name {
        height: auto;
        padding-right: 1;
    }

    /* Tall enough for a card whose body is capped at `APPROVAL_BODY_LINES`, plus
       the title, the rows, the footer and the border. The body cap is the tighter of
       the two on purpose: whatever this height clips is clipped from the *bottom*, and
       the bottom is where the answer you came to give is. */
    #cards {
        height: auto;
        max-height: 18;
        margin: 0 2;
        display: none;
    }

    .card {
        height: auto;
        padding: 0 1;
        border: round $primary;
        background: $surface;
    }

    .card OptionList {
        height: auto;
        max-height: 6;
        background: $surface;
        border: none;
        padding: 0;
        scrollbar-size-vertical: 1;
    }

    .card-body {
        height: auto;
        max-height: 11;
        color: $text-muted;
    }

    #prompt {
        height: auto;
        max-height: 10;
        margin: 0 2;
        border: round $primary;
        background: $surface;
    }

    #prompt:focus {
        border: round $accent;
    }

    #status {
        height: 1;
        padding: 0 3;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]
    """++shift+tab++ is not here: a focused `TextArea` claims it for focus movement
    before an app binding is reached, so the prompt posts `ModeCycle` instead."""

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        daemonless: bool = False,
        cwd: Path | None = None,
        resume: str = "",
        overrides: dict[str, Any] | None = None,
        terminal_mode: TerminalMode = "dark",
        prefs_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._config_path = config_path or default_config_path()
        self._first_run = not config_exists(self._config_path)
        self._stored = GatewayConfig() if self._first_run else load_config(self._config_path)
        """What is on disk, and the only thing ever written back.

        Kept apart from `self.config` because a flag must never become a stored default:
        folding `--transport tcp` into the config the UI later saves is how a container
        address typed once becomes the transport a bare `stcode` binds forever after.
        User-made changes are applied to **both** — that is what `_amend` is for.
        """

        self._overrides = overrides or {}
        self.config = apply_cli_overrides(self._stored, **self._overrides)
        # `--daemonless`: never start an agent here. Starting a local one would run the
        # work on this machine instead — the opposite of what was asked for.
        self._daemonless = daemonless
        self._cwd = cwd or Path.cwd()
        self._resume = resume
        self._prefs_path = prefs_path or ui_path()
        self.prefs: UiPrefs = load_prefs(self._prefs_path)
        self._terminal_mode = terminal_mode
        """Detected before the app started: Textual owns the tty afterwards, and two
        things reading escape sequences off one terminal is a corrupted screen."""

        self._client: DaemonClient | None = None
        # Set only when this process started the daemon — the one case where quitting
        # should stop it.
        self._daemon: Daemon | None = None
        self._session_id = ""
        self._info: dict[str, Any] = {}
        self._files: list[str] | None = None
        """Every path `@` can offer, or None while the workspace has not been listed.
        The distinction is the difference between an empty card and an honest one."""

        self._streams: dict[tuple[str, str], Message | Thinking | Progress] = {}
        self._tools: dict[tuple[str, str], ToolCall] = {}
        self._requests: deque[dict[str, Any]] = deque()
        self._working = False
        self._spinner = 0
        self._spinner_timer: Timer | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield Transcript(id="transcript")
        yield CardZone(id="cards")
        yield Prompt(labels.PROMPT_PLACEHOLDER)
        yield Static("", id="status")

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self._apply_theme()
        self._refresh_status()
        self.prompt.focus()
        self._start()

    @work
    async def _start(self) -> None:
        """First run, then trust, then connect — in that order, and each one can stop.

        A worker because two of the three are modals: `push_screen_wait` needs
        somewhere to suspend, and `on_mount` is not it.
        """
        if self._first_run:
            self._open_settings(first_run=True)
            return
        if not self.config.defaults.model:
            self._warn(labels.NO_MODEL_ERROR)
        if not await self._ensure_trusted():
            return
        self._connect()

    async def _ensure_trusted(self) -> bool:
        """Ask about this folder unless it is already trusted. False means leave.

        Skipped entirely in `--daemonless`: the workspace belongs to the daemon's
        machine, so there is nothing here to trust, and `/connect` is the question that
        actually matters. → docs/decisions/0003-what-the-tui-owns.md
        """
        if self._daemonless or self.prefs.is_trusted(self._cwd):
            return True
        if not await self.push_screen_wait(TrustScreen(self._cwd)):
            self.exit()
            return False
        self.prefs.trust(self._cwd)
        self._save_prefs()
        return True

    # ------------------------------------------------------------- convenience

    @property
    def prompt(self) -> Prompt:
        return self.query_one(Prompt)

    @property
    def transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    @property
    def cards(self) -> CardZone:
        return self.query_one("#cards", CardZone)

    # ------------------------------------------------------------------- daemon

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

    @work(exclusive=True, group="daemon")
    async def _connect(self, ask: bool = False) -> None:
        """Find or start a daemon, open a session, then render its frames forever.

        Exclusive, so calling it again after a settings change cancels the running pump
        first. The old connection closes here rather than there, because the worker that
        owned it is already cancelled by the time we run.
        """
        await self._close_connection()
        self._session_id = ""
        try:
            client, embedded = (
                (await self._connect_elsewhere(), False) if ask else await self._open_client()
            )
        except Exception as exc:  # noqa: BLE001 — including AutonomyRefused, which is a
            # refusal the user has to read: `full-auto` on the host does not start.
            self._error(labels.daemon_failed(exc))
            return

        self._client = client
        self._note(labels.daemon_connected(self._daemon_address(), embedded))
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

        self._adopt_session(info)
        self._note(labels.session_started(info.get("cwd", "")))
        # Fetched once, up front, so `?` and `/mcp` answer instantly — and because the
        # answers are facts about the *daemon's* machine, which this one cannot see.
        self._load_info()

        # `aclosing`, for the same reason `Agent.run` documents it: this worker is
        # cancelled when the app exits, and a generator left suspended is finalised
        # whenever the garbage collector gets to it — possibly after the event loop has
        # closed, which surfaces as an unraisable `Event loop is closed`.
        async with aclosing(client.events()) as frames:
            async for frame in frames:
                self._render(frame)

    async def _open_client(self) -> tuple[DaemonClient, bool]:
        """Connect, or start a daemon here and connect to that.

        The `OSError` is the interesting case, not a failure: nothing listening is
        exactly how "no daemon yet" looks. What it means depends on the mode — start
        one, or ask where the right one is.
        """
        if self._daemonless:
            # Ask first rather than probing: in this shape the address *is* the
            # question, and the stored one is a guess from a previous run.
            return await self._connect_elsewhere(), False
        try:
            return await DaemonClient.connect(self.config), False
        except OSError:
            pass
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
        reason = labels.CONNECT_INTRO
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
            # The next run should start where this one ended up. An address typed into
            # this screen is a decision, unlike `--host` — so it goes in the stored
            # config too, which is the one that gets written.
            self._stored.daemon = settings.model_copy(deep=True)
            self._persist()
            return client

    def _daemon_address(self) -> str:
        settings = self.config.daemon
        if settings.transport == "unix":
            return settings.socket
        return f"{settings.host}:{settings.port}"

    @work(group="info")
    async def _load_info(self) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._info = await self._client.info()
        self._refresh_status()

    # ---------------------------------------------------------------- rendering

    def _render(self, frame: dict[str, Any]) -> None:
        agent = str(frame.get("agent", ""))
        match frame.get("type"):
            case "text_delta":
                self._stream_into("assistant", str(frame.get("text", "")), agent)
            case "reasoning_delta":
                self._stream_into("thinking", str(frame.get("text", "")), agent)
            case "tool_started":
                self._tool_started(frame, agent)
            case "tool_finished":
                self._tool_finished(frame, agent)
            case "turn_finished":
                self._end_streams(agent)
                if not agent:
                    self._stop_working()
                    self._note(labels.turn_usage(dict(frame.get("usage", {}))))
            case "agent_failed":
                self._end_streams(agent)
                if not agent:
                    self._stop_working()
                self._error(str(frame.get("message", "")), agent=agent)
            case "approval_request" | "question":
                self._enqueue_request(frame)
            case "supervisor":
                self._end_streams(agent)
                self._note(labels.supervisor_nudge(str(frame.get("text", ""))))
            case "progress":
                self._stream_into("progress", str(frame.get("text", "")), agent)
            case "history":
                self._replay(frame)
            case "session":
                # The daemon is authoritative: a refused `set_mode` must not leave the
                # status line showing a mode that is not in force, nor reach the config.
                self._adopt_session(frame)
            case "error":
                self._stop_working()
                self._error(str(frame.get("message", "")))

    def _replay(self, frame: dict[str, Any]) -> None:
        """Render a re-attached session's transcript before its live stream arrives.

        Raw records, so this shows what happened rather than what the model was sent.
        """
        for record in frame.get("records", []):
            match record.get("type"):
                case "user":
                    self.transcript.add(Message("user", str(record.get("content", ""))))
                case "assistant" if record.get("content"):
                    self.transcript.add(Message("assistant", str(record.get("content", ""))))
                case "tool_call":
                    call = ToolCall(
                        str(record.get("name", "")), dict(record.get("arguments", {}))
                    )
                    self.transcript.add(call)
                    self._tools[("", str(record.get("id", "")))] = call
                case "tool_result":
                    call = self._tools.pop(("", str(record.get("id", ""))), None)
                    if call is not None:
                        call.finish(
                            ok=not record.get("is_error", False),
                            preview=str(record.get("content", "")),
                        )
                case "supervisor":
                    self._note(labels.supervisor_nudge(str(record.get("content", ""))))
                case "inbox":
                    self._note(
                        labels.inbox_message(
                            str(record.get("from", "")),
                            str(record.get("subject", "")),
                            [str(ref) for ref in record.get("refs", [])],
                        )
                    )
                case "error":
                    self._error(str(record.get("message", "")))
        self._settle()

    def _tool_started(self, frame: dict[str, Any], agent: str) -> None:
        self._end_streams(agent)
        call = ToolCall(str(frame.get("name", "")), dict(frame.get("arguments", {})))
        self.transcript.add(call, agent=agent)
        self._tools[(agent, str(frame.get("id", "")))] = call
        self._settle()

    def _tool_finished(self, frame: dict[str, Any], agent: str) -> None:
        call = self._tools.pop((agent, str(frame.get("id", ""))), None)
        if call is None:
            # A result with no box: the client attached mid-turn. One box saying what
            # came back beats silently dropping it.
            call = ToolCall(str(frame.get("name", "")), {})
            self.transcript.add(call, agent=agent)
        call.finish(ok=bool(frame.get("ok", True)), preview=str(frame.get("preview", "")))

    def _stream_into(self, kind: str, text: str, agent: str) -> None:
        """Append a delta, opening a new block when the kind of delta changes.

        Keyed by `(agent, kind)`: deltas carry no boundaries, so a different kind
        arriving is the only signal a block ended — and two sub-agents running in
        parallel must not stream into each other's block.

        `progress` is one of the kinds for the same reason the other two are: a REPL
        cell streams a line at a time, and one entry per line is a screen of entries
        for one tool call.
        """
        key = (agent, kind)
        block = self._streams.get(key)
        if block is None:
            for other in STREAM_KINDS:
                if other != kind:
                    self._close_stream((agent, other))
            block = _OPEN_STREAM[kind]()
            self.transcript.add(block, agent=agent)
            self._streams[key] = block
        block.append(text)
        self.transcript.scroll_end(animate=False)
        self._settle()

    def _end_streams(self, agent: str = "") -> None:
        for kind in STREAM_KINDS:
            self._close_stream((agent, kind))

    def _close_stream(self, key: tuple[str, str]) -> None:
        block = self._streams.pop(key, None)
        if block is not None and not block.body.strip():
            block.remove()

    def _settle(self) -> None:
        """Hide the banner once the transcript has outgrown the screen.

        After the refresh, not during it: mounting is queued, so `max_scroll_y` read
        immediately after adding an entry is the value from *before* that entry existed
        — and the banner would sit there through a whole conversation.
        """
        self.call_after_refresh(self._settle_now)

    def _settle_now(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#banner", Banner).follow(
                transcript_scrolls=self.transcript.scrolls
            )

    # ------------------------------------------------------------------ output

    def _note(self, body: str) -> None:
        """What the platform did, as a rule across the screen."""
        self._end_streams()
        self.transcript.add(PlatformNote(body))
        self._settle()

    def _warn(self, body: str) -> None:
        self.transcript.add(Notice(body, level="warning"))
        self._settle()

    def _error(self, body: str, *, agent: str = "") -> None:
        self._end_streams(agent)
        self.transcript.add(Notice(body, level="error"), agent=agent)
        self._settle()

    # ------------------------------------------------------------------- cards

    def _show_card(self, card: Card, *, hotkeys: dict[str, str] | None = None) -> Card:
        shown = self.cards.show(card)
        self.prompt.card_open = True
        self.prompt.card_hotkeys = dict(hotkeys or {})
        return shown

    def _close_card(self) -> None:
        self.cards.clear()
        self.prompt.card_open = False
        self.prompt.card_hotkeys = {}
        # A request that arrived while something else was open gets its turn now.
        self._show_next_request()

    @on(Prompt.HelpRequested)
    def _help_card(self) -> None:
        self._show_card(
            Card(
                labels.CARD_HELP,
                labels.help_rows(
                    config=str(self._config_path),
                    prefs=str(self._prefs_path),
                    session=str(self._info.get("session_path", "")),
                    daemon=str(self._info.get("daemon", "")),
                ),
                footer=labels.CARD_FOOTER_CLOSE,
                selectable=False,
            )
        )

    @on(Prompt.CardMove)
    def _card_move(self, message: Prompt.CardMove) -> None:
        card = self.cards.current
        if card is not None:
            card.move(message.delta)

    @on(Prompt.CardClose)
    def _card_close(self) -> None:
        self._close_card()

    @on(Prompt.CardHotkey)
    def _card_hotkey(self, message: Prompt.CardHotkey) -> None:
        card = self.cards.current
        if card is not None:
            card.choose_value(message.value)

    @on(Prompt.CardChoose)
    def _card_choose(self) -> None:
        card = self.cards.current
        if card is None:
            return
        # A question always accepts something the user typed instead: the tool's own
        # docstring promises the model that the answer may be none of the options.
        if card.kind == labels.CARD_QUESTION and self.prompt.text.strip():
            self._answer_question(card.token, self.prompt.clear_text().strip())
            self._close_card()
            return
        if not card.choose():
            self._close_card()

    @on(Card.Chosen)
    def _card_chosen(self, message: Card.Chosen) -> None:
        match message.kind:
            case labels.CARD_COMMANDS:
                self.prompt.clear_text()
                self._close_card()
                self._run_command(message.value)
                return
            case labels.CARD_FILES:
                self.prompt.insert_token("@", message.value)
            case labels.CARD_MODE:
                mode = parse_approval_mode(message.value)
                if mode is not None:
                    self._set_mode(mode)
            case labels.CARD_THEME:
                self._set_theme(message.value)  # type: ignore[arg-type]
            case labels.CARD_EFFORT:
                self._set_effort(message.value)
            case labels.CARD_APPROVE:
                self._answer_approval(message.token, message.value == "approve")
            case labels.CARD_QUESTION:
                self._answer_question(message.token, message.value)
            case labels.CARD_SKILLS:
                self.prompt.insert_token("/", labels.skill_command(message.value))
        self._close_card()

    @on(Prompt.Changed)
    def _prompt_changed(self) -> None:
        """Open, filter or close the `/` and `@` cards as the token under the cursor
        changes. The one place that decides is `token_trigger`.

        **Only those two.** A card a command opened is not driven by the text, and must
        not be closed by it: submitting `/effort` clears the input, and the `Changed`
        that clearing produces arrives *after* the effort card is up. Treating that as
        "no token under the cursor, so close whatever is open" is what made `/effort`,
        `/theme`, `/mode`, `/mcp` and `/skills` all appear to do nothing at all.
        """
        card = self.cards.current
        if card is not None and card.kind == labels.CARD_HELP:
            # `?` inserted nothing, so the second one is a literal question mark — and
            # the card it opened gets out of the way as soon as there is text.
            if self.prompt.text:
                self._close_card()
            return
        if card is not None and card.kind not in labels.TOKEN_CARDS:
            return  # A picker, an approval or a question keeps the floor until answered.
        trigger = token_trigger(self.prompt.text, self.prompt.cursor_offset)
        if trigger is None:
            if card is not None:
                self._close_card()
            return
        symbol, query = trigger
        kind = labels.CARD_COMMANDS if symbol == "/" else labels.CARD_FILES
        if symbol == "@" and self._files is None:
            # First `@` of the session: the listing runs in a thread, and the card that
            # opens now is empty. `_files_loaded` refills it when the thread returns —
            # without that, the first `@` showed nothing and only the second one worked,
            # which reads as a broken key rather than a slow one.
            self._load_files()
        rows, selectable = self._token_rows(symbol, query)
        if card is not None and card.kind == kind:
            card.selectable = selectable
            card.show(rows)
            return
        self._show_card(
            Card(kind, rows, footer=labels.CARD_FOOTER_CHOOSE, selectable=selectable)
        )

    def _token_rows(self, symbol: str, query: str) -> tuple[list[tuple[str, str]], bool]:
        """The rows for a `/` or `@` card, and whether any of them can be chosen.

        A card with nothing to offer still opens, carrying one row that says why. Not
        selectable, because "listing the workspace…" is a sentence and inserting it into
        the prompt as a path is not what pressing enter meant.
        """
        if symbol == "/":
            skills = [str(skill.get("name", "")) for skill in self._info.get("skills", [])]
            available = labels.commands_for(daemonless=self._daemonless, skills=skills)
            return filter_rows(available, query), True
        if self._files is None:
            return [(labels.FILES_LOADING, "")], False
        if not self._files:
            return [(labels.NO_FILES_HERE, "")], False
        return filter_rows(labels.file_rows(self._files), query), True

    # ---------------------------------------------------------------- approvals

    def _enqueue_request(self, frame: dict[str, Any]) -> None:
        """Queue an approval or a question, and show it if the floor is free.

        Parallel tool calls can raise two at once. Queued rather than stacked: two
        cards is two answers to give with one keyboard, and the second would be
        answering a question hidden behind the first.
        """
        self._end_streams()
        self._requests.append(frame)
        current = self.cards.current
        if current is None or current.kind not in (labels.CARD_APPROVE, labels.CARD_QUESTION):
            self._close_card_silently()
            self._show_next_request()

    def _close_card_silently(self) -> None:
        self.cards.clear()
        self.prompt.card_open = False
        self.prompt.card_hotkeys = {}

    def _show_next_request(self) -> None:
        if not self._requests or self.cards.current is not None:
            return
        frame = self._requests.popleft()
        if frame.get("type") == "approval_request":
            self._show_approval(frame)
        else:
            self._show_question(frame)

    def _show_approval(self, frame: dict[str, Any]) -> None:
        summary = labels.approval_summary(
            str(frame.get("tool", "")),
            str(frame.get("permission", "")),
            dict(frame.get("arguments", {})),
        )
        agent = str(frame.get("agent_name", "")) or ""
        title = labels.CARD_APPROVE if agent in ("", "main") else f"{labels.CARD_APPROVE} {agent}"
        self._show_card(
            Card(
                labels.CARD_APPROVE,
                # Deny first, so the highlighted row — and therefore a reflexive enter —
                # is the safe answer.
                [("deny", "the model is told you declined"), ("approve", "run it")],
                title=title,
                body=summary,
                footer=labels.CARD_FOOTER_APPROVE,
                token=str(frame.get("execution_id", "")),
            ),
            hotkeys={"y": "approve", "n": "deny"},
        )

    def _show_question(self, frame: dict[str, Any]) -> None:
        options = [str(option) for option in frame.get("options", [])]
        self._show_card(
            Card(
                labels.CARD_QUESTION,
                [(option, "") for option in options],
                title=str(frame.get("header", "")) or labels.CARD_QUESTION,
                body=str(frame.get("question", "")),
                footer=labels.CARD_FOOTER_QUESTION,
                token=str(frame.get("execution_id", "")),
                selectable=bool(options),
            )
        )

    @work(group="answers")
    async def _answer_approval(self, execution_id: str, approved: bool) -> None:
        if self._client is not None:
            await self._client.approve(execution_id, approved)
        if not approved:
            self._note(labels.APPROVAL_DENIED)

    @work(group="answers")
    async def _answer_question(self, execution_id: str, text: str) -> None:
        if self._client is not None:
            await self._client.answer(execution_id, text)

    # ------------------------------------------------------------------- files

    @work(thread=True, group="files", exclusive=True)
    def _load_files(self) -> None:
        """List the workspace once, in a thread.

        Exclusive: every keystroke while the listing is in flight asks again, and one
        `git ls-files` per character typed is a thread pool doing the same work six
        times to produce the same answer.

        `git ls-files` when the workspace is a repo — it already knows what is ignored,
        and reimplementing `.gitignore` to build a mention list would be a second
        opinion about which files exist. A plain walk otherwise.
        """
        root = Path(str(self._info.get("cwd", "") or self._cwd))
        # An empty list, not None: "listed, and there is nothing" is a different answer
        # from "not listed yet", and only one of them is worth saying out loud.
        found = _list_files(root) if root.is_dir() else []
        self.call_from_thread(self._files_loaded, found)

    def _files_loaded(self, found: list[str]) -> None:
        """Adopt the listing, and refill the card that opened before it existed."""
        self._files = found
        card = self.cards.current
        if card is None or card.kind != labels.CARD_FILES:
            return
        trigger = token_trigger(self.prompt.text, self.prompt.cursor_offset)
        rows, selectable = self._token_rows("@", trigger[1] if trigger else "")
        card.selectable = selectable
        card.show(rows)

    # ---------------------------------------------------------------- settings

    def _open_settings(self, *, first_run: bool) -> None:
        def saved(config: GatewayConfig | None) -> None:
            if config is None:
                if first_run:
                    self._warn(labels.SETUP_SKIPPED)
                return
            self._stored = config
            save_config(self._stored, self._config_path)
            # Saved first, then flags back on top: what the user typed belongs in the
            # file, what they passed on the command line belongs only to this run.
            self.config = apply_cli_overrides(self._stored, **self._overrides)
            self._first_run = False
            self._refresh_status()
            if not self.config.defaults.model:
                self._warn(labels.NO_MODEL_ERROR)
            if self._session_id:
                self._apply_settings()
            else:
                # First run: the daemon built its gateway from the old config, so new
                # credentials reach it only through a new connection.
                self._start()

        self.push_screen(SettingsScreen(self.config, first_run=first_run), saved)

    @work(group="answers")
    async def _apply_settings(self) -> None:
        """Make a saved `/model` true for the session that is already running.

        Two halves, because a model and a *credential* travel differently.

        The provider and model are session settings: they go in as a `meta` record, the
        agent re-reads its own meta before every call, and the change lands on the next
        one. Both, not just the model — switching provider and pushing only the model
        leaves the session routing a new model name at the old vendor.

        A key, a base URL, a routing tier or a concurrency cap is not a session setting;
        it belongs to the gateway, which the daemon built once at start-up from its own
        config. Reconfiguring it in place is the only thing that reaches an agent that
        is already holding it — and it is done **only for a daemon this process
        started**. A daemon somewhere else reads its own config file, and pushing this
        terminal's credentials at it would be this client deciding what another machine
        is configured with.
        """
        if self._daemon is not None:
            await self._daemon.reconfigure(self.config)
        elif self._daemonless:
            self._note(labels.REMOTE_CONFIG_UNCHANGED)
        if self._client is not None and self._session_id:
            await self._client.set_meta(
                provider=self.config.defaults.provider, model=self.config.defaults.model
            )

    # -------------------------------------------------------------------- mode

    @on(Prompt.ModeCycle)
    def action_cycle_mode(self) -> None:
        self._set_mode(next_approval_mode(self.config.defaults.approval_mode))

    def _set_mode(self, mode: ApprovalMode) -> None:
        """Ask for a mode. The daemon decides whether it gets one.

        Nothing is announced or written while a session is attached: the daemon can
        refuse `full-auto` on the host, and announcing first would both lie and
        *persist* a mode it will refuse to start in next time. The `session` frame it
        sends back is the answer, and `_adopt_session` is where a confirmed change lands.
        """
        if self._client is not None and self._session_id:
            self._push_mode(mode)
            return
        self._amend(approval_mode=mode)
        self._refresh_status()
        self._note(labels.mode_changed(mode))

    def _adopt_session(self, frame: dict[str, Any]) -> None:
        """Take what the daemon reports as the truth, and only then write it down."""
        session_id = str(frame.get("id", ""))
        if session_id:
            self._session_id = session_id
        mode = str(frame.get("approval_mode", ""))
        if mode and mode != self.config.defaults.approval_mode:
            self._amend(approval_mode=mode)
            self._note(labels.mode_changed(mode))  # type: ignore[arg-type]
        self._effective = {
            "model": str(frame.get("model", "")),
            "provider": str(frame.get("provider", "")),
            "effort": str(frame.get("reasoning_effort", "")),
        }
        self._refresh_status()

    def _persist(self) -> None:
        """Only once a config file exists; skipping setup should not create one.

        Writes `_stored`, never `config`: `config` is `_stored` with this run's flags
        folded in, and saving that is how `--mode full-auto` used once becomes the mode
        in the file forever.
        """
        if config_exists(self._config_path):
            save_config(self._stored, self._config_path)

    def _amend(self, **fields: Any) -> None:
        """Record a change the *user* made, in both the live config and the stored one.

        The split exists to keep command-line flags out of the file; a choice made in
        the UI is the opposite case and belongs in both. Flags still win for this run —
        they are reapplied on top — so `--mode plan` is not undone by a `/mode` that the
        daemon then refuses.
        """
        for name, value in fields.items():
            setattr(self.config.defaults, name, value)
            setattr(self._stored.defaults, name, value)
        self._persist()

    def _save_prefs(self) -> None:
        with contextlib.suppress(OSError):
            save_prefs(self.prefs, self._prefs_path)

    @work(group="answers")
    async def _push_mode(self, mode: ApprovalMode) -> None:
        """Tell the live session, so `/mode` means something before the next session."""
        if self._client is not None and self._session_id:
            await self._client.set_mode(mode)

    @work(group="answers")
    async def _push_meta(self, **fields: Any) -> None:
        """`/model` and `/effort`, as a `meta` record on the live session."""
        if self._client is not None and self._session_id:
            await self._client.set_meta(**fields)

    # ------------------------------------------------------------------- theme

    def _apply_theme(self) -> None:
        self.theme = theme_name_for(self.prefs.theme, detected=self._terminal_mode)

    def _set_theme(self, preference: ThemePreference) -> None:
        self.prefs.theme = preference
        self._apply_theme()
        self._save_prefs()
        self._note(labels.theme_changed(preference))

    def _set_effort(self, effort: str) -> None:
        """Change the effort, and remember it.

        Both halves matter and they are different mechanisms. The `meta` record reaches
        the session that is running — that is what `/effort` is *for*. Writing
        `[defaults] reasoning_effort` is what makes it survive: without it the config
        the CLI reads next time still says nothing, and the choice quietly reverts at
        the next `stcode`, which is exactly what it looked like when this did only the
        first half.
        """
        self._amend(reasoning_effort=effort)
        self._refresh_status()
        if self._client is None or not self._session_id:
            self._error(labels.not_connected())
            return
        self._push_meta(reasoning_effort=effort)
        self._note(labels.effort_changed(effort))

    # ------------------------------------------------------------- status line

    def _refresh_status(self) -> None:
        effective = getattr(self, "_effective", {})
        defaults = self.config.defaults
        mode = defaults.approval_mode
        model = effective.get("model") or defaults.model
        provider = effective.get("provider") or defaults.provider

        text = Text()
        if self._working:
            frame = labels.SPINNER_FRAMES[self._spinner % len(labels.SPINNER_FRAMES)]
            text.append(labels.working_status(frame), style=f"bold {PRIMARY}")
        text.append("model ", style="dim")
        text.append(model or labels.STATUS_NO_MODEL, style="bold" if model else "bold $warning")
        text.append("   provider ", style="dim")
        text.append(provider)
        text.append("   mode ", style="dim")
        text.append(mode, style=f"bold {labels.APPROVAL_MODE_COLOR.get(mode, 'white')}")
        effort = effective.get("effort") or defaults.reasoning_effort
        if effort:
            text.append("   effort ", style="dim")
            text.append(str(effort))
        if self._daemonless:
            # The one shape where "which agent am I talking to" is a live question.
            text.append("   daemon ", style="dim")
            text.append(self._daemon_address())
        with contextlib.suppress(Exception):
            self.query_one("#status", Static).update(text)

    def _effective_provider(self) -> str:
        """What the session is actually using, falling back to the configured default.

        The daemon is authoritative here as everywhere: after a `/model` the session's
        provider and the config's can differ for one call, and the card should describe
        the one the next request will go to.
        """
        effective = getattr(self, "_effective", {})
        return str(effective.get("provider") or self.config.defaults.provider)

    def _effective_effort(self) -> str:
        effective = getattr(self, "_effective", {})
        return str(effective.get("effort") or self.config.defaults.reasoning_effort)

    def _skill_names(self) -> dict[str, str]:
        """What this session's daemon found, as `typed -> as the registry spells it`.

        The daemon's machine is the one that knows — in `--daemonless` the skills are
        not on this one. Both spellings are kept because `/` commands are matched
        case-insensitively and the registry looks skills up exactly: sending the
        lowercased token would mean a skill named `PDF` could be offered and then not
        found.
        """
        found: dict[str, str] = {}
        for skill in self._info.get("skills", []):
            name = str(skill.get("name", ""))
            if name:
                found.setdefault(name.lower(), name)
        return found

    def _has_credential(self) -> bool:
        provider = self.config.providers.get(self.config.defaults.provider, ProviderConfig())
        return bool(resolve_secret(provider.api_key_env, provider.api_key))

    # ---------------------------------------------------------------- commands

    @on(Prompt.Submitted)
    def _on_submit(self, message: Prompt.Submitted) -> None:
        text = message.text.strip()
        self.prompt.clear_text()
        if not text:
            return
        if text.startswith(labels.SHELL_PREFIX):
            self._run_shell(text[1:].strip())
        elif text.startswith("/"):
            self._run_command(text)
        else:
            self._send(text)

    # ------------------------------------------------------------------- `!` shell

    def _run_shell(self, command: str) -> None:
        """Run one command here, in this terminal, and show what it printed.

        **Nothing about this touches the agent.** It is not a tool call, it is not
        approved, and it is not written to the session — so `!git status` before you
        describe a change costs no context and leaves no record the model will later
        read back as something it did. The transcript entry is a rule down the left for
        exactly that reason: it has to be impossible to mistake for the agent's work.

        It runs in *this* process's workspace, not the daemon's. In `--daemonless` those
        are different machines, and `!` is always the near one — which is the honest
        answer, because this is your shell and not the agent's.
        """
        if not command:
            self._warn(labels.SHELL_NO_COMMAND)
            return
        entry = ShellOutput(command)
        self.transcript.add(entry)
        self._settle()
        self._shell_worker(entry, command)

    @work(thread=True, group="shell")
    def _shell_worker(self, entry: ShellOutput, command: str) -> None:
        """In a thread, because `subprocess.run` blocks and the turn behind it must not.

        Bounded by `shell_timeout` from `ui.toml` — a `!` that hangs would otherwise
        hold a worker open for as long as the command felt like running, and killing it
        is a message rather than a mystery.
        """
        timeout = clamp_shell_timeout(self.prefs.shell_timeout)
        # `self._cwd`, deliberately, and not the daemon's: in `--daemonless` the agent's
        # workspace is on another machine and there is nothing here to run a command in.
        try:
            finished = subprocess.run(
                command,
                shell=True,  # noqa: S602 — the whole feature is "run this in my shell"
                cwd=self._cwd if self._cwd.is_dir() else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            output, code = labels.shell_timed_out(timeout), 124
        except (OSError, ValueError) as exc:
            output, code = labels.shell_failed(exc), 1
        else:
            # stderr under stdout rather than interleaved: the ordering between two
            # pipes is not something we can recover, and pretending otherwise would
            # invent a sequence the command never produced.
            parts = [part for part in (finished.stdout, finished.stderr) if part.strip()]
            output, code = "\n".join(part.rstrip() for part in parts), finished.returncode
        self.call_from_thread(entry.finish, output, code)
        self.call_from_thread(self._settle)

    def _run_command(self, raw: str) -> None:
        name, _, argument = raw[1:].partition(" ")
        name = name.lower()
        argument = argument.strip()

        match name:
            case "quit" | "exit" | "q":
                self.exit()
            case "model":
                self._open_settings(first_run=False)
            case "effort":
                # Rows for the provider actually in force, so a rung this one clamps
                # says so rather than silently doing what the rung below it does.
                rows = labels.effort_rows(self._effective_provider())
                current = self._effective_effort()
                self._show_card(
                    Card(
                        labels.CARD_EFFORT,
                        rows,
                        footer=labels.CARD_FOOTER_CHOOSE,
                        highlighted=next(
                            (index for index, row in enumerate(rows) if row[0] == current), 0
                        ),
                    )
                )
            case "mode" if argument:
                mode = parse_approval_mode(argument)
                if mode is None:
                    self._error(labels.unknown_mode(argument))
                else:
                    self._set_mode(mode)
            case "mode":
                self._show_card(
                    Card(labels.CARD_MODE, labels.mode_rows(), footer=labels.CARD_FOOTER_CHOOSE)
                )
            case "theme":
                self._show_card(
                    Card(labels.CARD_THEME, labels.THEME_ROWS, footer=labels.CARD_FOOTER_CHOOSE)
                )
            case "connect" if not self._daemonless:
                self._error(labels.daemonless_only(name))
            case "connect":
                self._connect(ask=True)
            case "sessions":
                self._pick_session()
            case "clear":
                self._clear_session()
            case "mcp":
                servers = list(self._info.get("mcp", []))
                self._show_card(
                    Card(
                        labels.CARD_MCP,
                        labels.mcp_rows(servers) or [(labels.NO_MCP_SERVERS, "")],
                        footer=labels.CARD_FOOTER_CLOSE,
                        selectable=False,
                    )
                )
            case "skills":
                skills = list(self._info.get("skills", []))
                self._show_card(
                    Card(
                        labels.CARD_SKILLS,
                        labels.skill_rows(skills) or [(labels.NO_SKILLS, "")],
                        footer=labels.CARD_FOOTER_CHOOSE if skills else labels.CARD_FOOTER_CLOSE,
                        # Choosing one writes `/name` into the input rather than running
                        # it: a skill usually needs a sentence after it saying what to
                        # do, and a card that fired on enter would leave nowhere to put
                        # it. The name is the part that is tedious to type correctly.
                        selectable=bool(skills),
                    )
                )
            case "token" | "tokens":
                self._show_tokens()
            case "help":
                self._help_card()
            case _ if name in self._skill_names():
                self._send(labels.skill_request(self._skill_names()[name], argument))
            case _:
                self._error(labels.unknown_command(name))

    @work(group="info")
    async def _show_tokens(self) -> None:
        """What this session has spent, asked for fresh.

        Fetched rather than remembered: the totals live in the session's `usage`
        records, one per model call, and the daemon is the only thing that has all of
        them — a client that attached halfway through watched half a conversation.
        """
        if self._client is None or not self._session_id:
            self._error(labels.not_connected())
            return
        try:
            self._info = await self._client.info()
        except Exception as exc:  # noqa: BLE001 — a card that cannot be filled says why
            self._error(labels.daemon_failed(exc))
            return
        self._show_card(
            Card(
                labels.CARD_TOKENS,
                labels.token_rows(dict(self._info.get("usage", {}))),
                footer=labels.TOKEN_FOOTER,
                selectable=False,
            )
        )

    @work(group="sessions")
    async def _pick_session(self) -> None:
        """Show the tree, then attach to whatever was picked.

        Detach and clear before attaching: the `history` frame that follows is the whole
        transcript, and rendering it under the previous conversation would read as one
        session that changed subject.
        """
        if self._client is None:
            self._error(labels.not_connected())
            return
        rows = await self._client.sessions(50)
        chosen = await self.push_screen_wait(SessionsScreen(rows))
        if not chosen or chosen == self._session_id:
            return
        await self._client.detach(self._session_id)
        self._reset_view()
        info = await self._client.attach(chosen)
        self._adopt_session(info)
        self._note(labels.session_resumed(chosen))

    @work(group="sessions")
    async def _clear_session(self) -> None:
        """End this session and start another. Nothing is erased.

        A `create`, which is why this is cheap: a session file is not written until its
        first message, so clearing twice leaves no debris and the daemon drops what it
        was holding. → docs/decisions/0003-what-the-tui-owns.md
        """
        if self._client is None:
            self._error(labels.not_connected())
            return
        previous = self._session_id
        info = await self._client.create(
            cwd=self._cwd, approval_mode=self.config.defaults.approval_mode
        )
        if previous:
            await self._client.detach(previous)
        self._reset_view()
        self._adopt_session(info)
        self._note(labels.session_cleared())

    def _reset_view(self) -> None:
        """Clear the *view* and the handles into it. The session files are untouched."""
        self.transcript.clear()
        self._streams.clear()
        self._tools.clear()
        self._requests.clear()
        self._close_card_silently()
        self._settle()

    # ------------------------------------------------------------------- reply

    def _send(self, text: str) -> None:
        defaults = self.config.defaults
        self.transcript.add(Message("user", text))
        self._settle()
        if not defaults.model:
            self._error(labels.NO_MODEL_ERROR)
            return
        if not self._has_credential():
            self._error(labels.no_key_error(defaults.provider))
            return
        if self._client is None or not self._session_id:
            self._error(labels.not_connected())
            return
        self._start_working()
        self._push(text)

    # ------------------------------------------------------------------ working

    def _start_working(self) -> None:
        """Spin, from the moment the message is queued.

        The first token can be seconds away — a cold local model, a long system prompt,
        a retry — and until it arrives the screen is identical to one where nothing
        happened. A spinner in the status line is the difference between "it is
        thinking" and "did that send?", which is the only question anybody has in that
        gap.
        """
        if self._working:
            return
        self._working = True
        self._spinner = 0
        self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self._tick_spinner)
        self._refresh_status()

    def _tick_spinner(self) -> None:
        self._spinner += 1
        self._refresh_status()

    def _stop_working(self) -> None:
        if not self._working:
            return
        self._working = False
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._refresh_status()

    @work(group="push")
    async def _push(self, text: str) -> None:
        """Queue the message. A push arriving mid-turn waits for the next tool
        boundary; it never splices into the model call in flight."""
        if self._client is not None:
            await self._client.push(text)

    @on(Prompt.Interrupted)
    def _on_interrupt(self) -> None:
        """Esc stops the agent. `push` never does; this is the message that does."""
        self._interrupt()

    @work(group="answers")
    async def _interrupt(self) -> None:
        if self._client is not None and self._session_id:
            await self._client.interrupt()
            self._note(labels.STREAM_STOPPED)


def _list_files(root: Path, limit: int = FILE_LIMIT) -> list[str]:
    """Workspace paths for `@`, relative to `root`, newest convention first: git."""
    try:
        finished = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if finished.returncode == 0 and finished.stdout.strip():
            return sorted(finished.stdout.split("\n"))[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    return _walk(root, limit)


def _walk(root: Path, limit: int) -> list[str]:
    """Every file under `root`, dot-directories skipped and bounded.

    Bounded because this runs on a directory nobody vetted: a home directory, or a
    workspace with a 40,000-file build output in it, must not turn typing `@` into a
    minute of walking.
    """
    found: list[str] = []
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            found.append(str(Path(current, filename).relative_to(root)))
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)


def run(
    config_path: Path | None = None,
    *,
    daemonless: bool = False,
    cwd: Path | None = None,
    resume: str = "",
    overrides: dict[str, Any] | None = None,
) -> None:
    """Detect the terminal's theme, then hand it the screen.

    Detection first and outside the app: Textual owns the tty once it starts, and two
    things reading raw escape sequences off one terminal is a race whose loser is a
    corrupted screen.
    """
    StcodeApp(
        config_path=config_path,
        daemonless=daemonless,
        cwd=cwd,
        resume=resume,
        overrides=overrides,
        terminal_mode=detect_terminal_mode(),
    ).run()


__all__ = ["FILE_LIMIT", "SPINNER_INTERVAL", "StcodeApp", "run"]
