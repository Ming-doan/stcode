"""Chat page: user interaction and screen composition."""

from __future__ import annotations
import contextlib
from pathlib import Path
from typing import Any, Callable
from textual import on, work
from textual.app import ComposeResult
from textual.screen import Screen
from stcode.cli import labels
from stcode.cli.models import ApprovalMode, ThemePreference, StartOptions, UiPrefs
from stcode.cli.logic.commands import (
    next_approval_mode,
    parse_approval_mode,
    parse_command,
    skill_names,
)
from stcode.cli.logic.completion import filter_rows, token_trigger
from stcode.cli.logic.connection import Connection
from stcode.cli.logic.requests import RequestQueue
from stcode.cli.logic.sessions import Sessions
from stcode.cli.logic.settings import Settings
from stcode.cli.logic.state import ClientState
from stcode.cli.logic.workspace import Workspace
from stcode.cli.ui.components.banner import Banner
from stcode.cli.ui.components.cards import Card, CardZone
from stcode.cli.ui.components.prompt import Prompt
from stcode.cli.ui.components.status import Status
from stcode.cli.ui.components.transcript import Message, ShellOutput, Transcript
from stcode.cli.ui.screens.sessions import SessionsScreen
from stcode.cli.ui.screens.settings import SettingsScreen
from .presenter import ChatPresenter


class ChatScreen(Screen[None]):
    CSS_PATH = "chat.tcss"

    def __init__(
        self,
        connection: Connection,
        state: ClientState,
        options: StartOptions,
        prefs: UiPrefs,
        prefs_path: Path,
        *,
        request_connection: Callable[[bool], None],
        choose_theme: Callable[[ThemePreference], None],
    ) -> None:
        super().__init__()
        self.connection, self.state, self.options = connection, state, options
        self.prefs, self.prefs_path = prefs, prefs_path
        self.sessions = Sessions(connection, state, options)
        self.settings = Settings(state)
        self.workspace = Workspace(options, state, prefs)
        self.requests = RequestQueue()
        self.files: list[str] | None = None
        self._request_connection = request_connection
        self._choose_theme = choose_theme
        self._banner = Banner(id="banner")
        self._transcript = Transcript(id="transcript")
        self.presenter = ChatPresenter(self._transcript, self._banner)
        self.status = Status(
            state, daemonless=options.daemonless, address=self._daemon_address
        )

    def compose(self) -> ComposeResult:
        yield self._banner
        yield self._transcript
        yield CardZone(id="cards")
        yield Prompt(labels.PROMPT_PLACEHOLDER)
        yield self.status

    def on_mount(self) -> None:
        self.prompt.focus()

    def _daemon_address(self) -> str:
        return str(self.connection.address)

    def handle_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "session":
            self._adopt_session(frame)
        elif kind in ("approval_request", "question"):
            self._enqueue_request(frame)
        else:
            if kind == "error" or (
                kind in ("turn_finished", "agent_failed") and not frame.get("agent")
            ):
                self.status.stop_working()
            self.presenter.render(frame)

    @work(group="info")
    async def _load_info(self) -> None:
        if self.connection.client is not None:
            with contextlib.suppress(Exception):
                await self.sessions.load_info()
        self.status.refresh_display()

    @property
    def prompt(self) -> Prompt:
        return self.query_one(Prompt)

    @property
    def transcript(self) -> Transcript:
        return self.query_one("#transcript", Transcript)

    @property
    def cards(self) -> CardZone:
        return self.query_one("#cards", CardZone)

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
                    # The daemon's, when that is the one on screen: a path pointing at
                    # this laptop while the agent is in a container is a wrong answer.
                    config=str(self.state.config.get("path", "")),
                    prefs=str(self.prefs_path),
                    session=str(self.state.info.get("session_path", "")),
                    daemon=str(self.state.info.get("daemon", "")),
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
        card = self.cards.current
        if card is not None and not card.dismissable:
            # An approval or a question keeps the floor until it is answered. → `Card`
            return
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
        if not card.choose() and card.dismissable:
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
        if symbol == "@" and self.files is None:
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

    def _token_rows(
        self, symbol: str, query: str
    ) -> tuple[list[tuple[str, str]], bool]:
        """The rows for a `/` or `@` card, and whether any of them can be chosen.

        A card with nothing to offer still opens, carrying one row that says why. Not
        selectable, because "listing the workspace…" is a sentence and inserting it into
        the prompt as a path is not what pressing enter meant.
        """
        if symbol == "/":
            skills = [
                str(skill.get("name", ""))
                for skill in self.state.info.get("skills", [])
            ]
            available = labels.commands_for(
                daemonless=self.options.daemonless, skills=skills
            )
            return filter_rows(available, query), True
        if self.files is None:
            return [(labels.FILES_LOADING, "")], False
        if not self.files:
            return [(labels.NO_FILES_HERE, "")], False
        return filter_rows(labels.file_rows(self.files), query), True

    def _enqueue_request(self, frame: dict[str, Any]) -> None:
        """Queue an approval or a question, and show it if the floor is free.

        Parallel tool calls can raise two at once. Queued rather than stacked: two
        cards is two answers to give with one keyboard, and the second would be
        answering a question hidden behind the first.
        """
        self.presenter.end_streams()
        self.requests.append(frame)
        current = self.cards.current
        if current is None or current.kind not in (
            labels.CARD_APPROVE,
            labels.CARD_QUESTION,
        ):
            self._close_card_silently()
            self._show_next_request()

    def _close_card_silently(self) -> None:
        self.cards.clear()
        self.prompt.card_open = False
        self.prompt.card_hotkeys = {}

    def _show_next_request(self) -> None:
        if self.cards.current is not None:
            return
        frame = self.requests.pop()
        if frame is None:
            return
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
        title = (
            labels.CARD_APPROVE
            if agent in ("", "main")
            else f"{labels.CARD_APPROVE} {agent}"
        )
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
                dismissable=False,
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
                dismissable=False,
            )
        )

    @work(group="answers")
    async def _answer_approval(self, execution_id: str, approved: bool) -> None:
        if self.connection.client is not None:
            await self.connection.client.approve(execution_id, approved)
        if not approved:
            self.presenter.note(labels.APPROVAL_DENIED)

    @work(group="answers")
    async def _answer_question(self, execution_id: str, text: str) -> None:
        if self.connection.client is not None:
            await self.connection.client.answer(execution_id, text)

    def _files_loaded(self, found: list[str]) -> None:
        """Adopt the listing, and refill the card that opened before it existed."""
        self.files = found
        card = self.cards.current
        if card is None or card.kind != labels.CARD_FILES:
            return
        trigger = token_trigger(self.prompt.text, self.prompt.cursor_offset)
        rows, selectable = self._token_rows("@", trigger[1] if trigger else "")
        card.selectable = selectable
        card.show(rows)

    def _open_settings(self, *, first_run: bool) -> None:
        if not self.state.config:
            self.presenter.error(labels.not_connected())
            return

        def saved(patch: dict[str, Any] | None) -> None:
            if patch is None:
                if first_run:
                    self.presenter.warn(labels.SETUP_SKIPPED)
                return
            self._apply_settings(patch)

        self.app.push_screen(
            SettingsScreen(self.state.config, first_run=first_run), saved
        )

    @on(Prompt.ModeCycle)
    def action_cycle_mode(self) -> None:
        self._set_mode(
            next_approval_mode(
                self.state.effective.get("mode")
                or str(self.state.config.get("approval_mode", ""))
            )
        )

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
            self.presenter.warn(labels.SHELL_NO_COMMAND)
            return
        entry = ShellOutput(command)
        self.transcript.add(entry)
        self.presenter.settle()
        self._shell_worker(entry, command)

    def _run_command(self, raw: str) -> None:
        name, argument = parse_command(raw)

        match name:
            case "quit" | "exit" | "q":
                self.app.exit()
            case "model":
                self._open_settings(first_run=False)
            case "effort":
                # Rows for the provider actually in force, so a rung this one clamps
                # says so rather than silently doing what the rung below it does.
                rows = labels.effort_rows(
                    str(self.state.provider_entry.get("provider", ""))
                )
                current = self.state.effort
                self._show_card(
                    Card(
                        labels.CARD_EFFORT,
                        rows,
                        footer=labels.CARD_FOOTER_CHOOSE,
                        highlighted=next(
                            (
                                index
                                for index, row in enumerate(rows)
                                if row[0] == current
                            ),
                            0,
                        ),
                    )
                )
            case "mode" if argument:
                mode = parse_approval_mode(argument)
                if mode is None:
                    self.presenter.error(labels.unknown_mode(argument))
                else:
                    self._set_mode(mode)
            case "mode":
                self._show_card(
                    Card(
                        labels.CARD_MODE,
                        labels.mode_rows(),
                        footer=labels.CARD_FOOTER_CHOOSE,
                    )
                )
            case "theme":
                self._show_card(
                    Card(
                        labels.CARD_THEME,
                        labels.THEME_ROWS,
                        footer=labels.CARD_FOOTER_CHOOSE,
                    )
                )
            case "connect" if not self.options.daemonless:
                self.presenter.error(labels.daemonless_only(name))
            case "connect":
                self._request_connection(True)
            case "sessions":
                self._pick_session()
            case "clear":
                self._clear_session()
            case "mcp":
                servers = list(self.state.info.get("mcp", []))
                self._show_card(
                    Card(
                        labels.CARD_MCP,
                        labels.mcp_rows(servers) or [(labels.NO_MCP_SERVERS, "")],
                        footer=labels.CARD_FOOTER_CLOSE,
                        selectable=False,
                    )
                )
            case "skills":
                skills = list(self.state.info.get("skills", []))
                self._show_card(
                    Card(
                        labels.CARD_SKILLS,
                        labels.skill_rows(skills) or [(labels.NO_SKILLS, "")],
                        footer=labels.CARD_FOOTER_CHOOSE
                        if skills
                        else labels.CARD_FOOTER_CLOSE,
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
            case _ if name in skill_names(self.state.info):
                self._send(
                    labels.skill_request(skill_names(self.state.info)[name], argument)
                )
            case _:
                self.presenter.error(labels.unknown_command(name))

    @work(group="info")
    async def _show_tokens(self) -> None:
        """What this session has spent, asked for fresh.

        Fetched rather than remembered: the totals live in the session's `usage`
        records, one per model call, and the daemon is the only thing that has all of
        them — a client that attached halfway through watched half a conversation.
        """
        if self.connection.client is None or not self.state.session_id:
            self.presenter.error(labels.not_connected())
            return
        try:
            await self.sessions.load_info()
        except Exception as exc:  # noqa: BLE001 — a card that cannot be filled says why
            self.presenter.error(labels.daemon_failed(exc))
            return
        self._show_card(
            Card(
                labels.CARD_TOKENS,
                labels.token_rows(dict(self.state.info.get("usage", {}))),
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
        if self.connection.client is None:
            self.presenter.error(labels.not_connected())
            return
        rows = await self.sessions.list()
        chosen = await self.app.push_screen_wait(SessionsScreen(rows))
        if not chosen or chosen == self.state.session_id:
            return
        await self.sessions.detach()
        self._reset_view()
        info = await self.sessions.attach(chosen)
        self._adopt_session(info)
        self.presenter.note(labels.session_resumed(chosen))

    @work(group="sessions")
    async def _clear_session(self) -> None:
        """End this session and start another. Nothing is erased.

        A `create`, which is why this is cheap: a session file is not written until its
        first message, so clearing twice leaves no debris and the daemon drops what it
        was holding. → docs/decisions/0003-what-the-tui-owns.md
        """
        if self.connection.client is None:
            self.presenter.error(labels.not_connected())
            return
        info = await self.sessions.clear()
        self._reset_view()
        self._adopt_session(info)
        self.presenter.note(labels.session_cleared())

    def _send(self, text: str) -> None:
        self.transcript.add(Message("user", text))
        self.presenter.settle()
        if not self.state.has_credential:
            self.presenter.error(labels.no_key_error(self.state.provider))
            return
        if self.connection.client is None or not self.state.session_id:
            self.presenter.error(labels.not_connected())
            return
        self.status.start_working()
        self._push(text)

    @work(group="push")
    async def _push(self, text: str) -> None:
        """Queue the message. A push arriving mid-turn waits for the next tool
        boundary; it never splices into the model call in flight."""
        if self.connection.client is not None:
            await self.sessions.push(text)

    @on(Prompt.Interrupted)
    def _on_interrupt(self) -> None:
        """Esc stops the agent. `push` never does; this is the message that does."""
        self._interrupt()

    @work(group="answers")
    async def _interrupt(self) -> None:
        if self.connection.client is not None and self.state.session_id:
            await self.sessions.interrupt()
            self.presenter.note(labels.STREAM_STOPPED)

    @work(thread=True, group="files", exclusive=True)
    def _load_files(self) -> None:
        found = self.workspace.files()
        self.app.call_from_thread(self._files_loaded, found)

    @work(thread=True, group="shell")
    def _shell_worker(self, entry: ShellOutput, command: str) -> None:
        result = self.workspace.shell(command)
        output = result.output
        if result.timed_out is not None:
            output = labels.shell_timed_out(result.timed_out)
        elif result.error is not None:
            output = labels.shell_failed(result.error)
        self.app.call_from_thread(entry.finish, output, result.exit_code)
        self.app.call_from_thread(self.presenter.settle)

    @work(group="answers")
    async def _apply_settings(self, patch: dict[str, Any]) -> None:
        try:
            await self.settings.apply(self.sessions.client, patch)
        except Exception as exc:
            self.presenter.error(labels.daemon_failed(exc))
        else:
            self.presenter.note(
                labels.model_changed(str(self.state.config.get("model", "")))
            )
        finally:
            self.status.refresh_display()

    @work(group="answers")
    async def _set_mode(self, mode: ApprovalMode) -> None:
        try:
            await self.settings.set_mode(self.sessions.client, mode)
        except Exception as exc:
            self.presenter.error(labels.daemon_failed(exc))
        finally:
            self.status.refresh_display()

    @work(group="answers")
    async def _set_effort(self, effort: str) -> None:
        try:
            await self.settings.set_effort(self.sessions.client, effort)
        except Exception as exc:
            self.presenter.error(labels.daemon_failed(exc))
        else:
            self.presenter.note(labels.effort_changed(effort))
        finally:
            self.status.refresh_display()

    def _adopt_session(self, frame: dict[str, Any]) -> None:
        previous_mode = self.state.effective.get("mode")
        self.state.adopt_session(frame)
        mode = self.state.effective.get("mode", "")
        if mode and previous_mode and mode != previous_mode:
            self.presenter.note(labels.mode_changed(mode))
        self.status.refresh_display()

    def _set_theme(self, preference: ThemePreference) -> None:
        self._choose_theme(preference)
        self.presenter.note(labels.theme_changed(preference))

    def _reset_view(self) -> None:
        self.presenter.clear()
        self.requests.clear()
        self._close_card_silently()
        self.status.stop_working()
