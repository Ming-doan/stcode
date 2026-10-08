"""Application lifecycle and the app-owned daemon stream."""

from __future__ import annotations

import contextlib
from contextlib import aclosing
from pathlib import Path
from typing import Callable, ClassVar

from textual import work
from textual.app import App
from textual.binding import Binding, BindingType

from stcode.cli import labels
from stcode.cli.logic.connection import Connection
from stcode.cli.logic.state import ClientState
from stcode.cli.models import StartOptions, ThemePreference, UiPrefs
from stcode.cli.ui.screens.chat.screen import ChatScreen
from stcode.cli.ui.screens.connect import ConnectScreen
from stcode.cli.ui.screens.trust import TrustScreen
from stcode.cli.ui.theme import THEMES, TerminalMode, theme_name_for


class StcodeApp(App[None]):
    TITLE = "stcode"
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True)
    ]

    def __init__(
        self,
        connection: Connection,
        state: ClientState,
        options: StartOptions,
        prefs: UiPrefs,
        prefs_path: Path,
        *,
        save_preferences: Callable[[], None],
        restart: bool = False,
        terminal_mode: TerminalMode = "dark",
    ) -> None:
        super().__init__()
        self.connection, self.state, self.options = connection, state, options
        self.prefs = prefs
        self._save_preferences = save_preferences
        self._restart = restart
        self._terminal_mode = terminal_mode
        self.chat = ChatScreen(
            connection,
            state,
            options,
            prefs,
            prefs_path,
            request_connection=self.connect,
            choose_theme=self.set_theme,
        )

    def get_default_screen(self) -> ChatScreen:
        return self.chat

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = theme_name_for(self.prefs.theme, detected=self._terminal_mode)
        self._start()

    @work
    async def _start(self) -> None:
        if not self.options.daemonless and not self.prefs.is_trusted(self.options.cwd):
            if not await self.push_screen_wait(TrustScreen(self.options.cwd)):
                self.exit()
                return
            self.prefs.trust(self.options.cwd)
            self._save_preferences()
        self.connect()

    def set_theme(self, preference: ThemePreference) -> None:
        self.prefs.theme = preference
        self.theme = theme_name_for(preference, detected=self._terminal_mode)
        self._save_preferences()

    async def on_unmount(self) -> None:
        with contextlib.suppress(Exception):
            await self.connection.aclose()

    @work(exclusive=True, group="daemon")
    async def connect(self, ask: bool = False) -> None:
        """Keep the pump alive across modal screens; reconnect cancels its predecessor."""
        await self.connection.close_client()
        self.state.session_id = ""
        try:
            if ask or self.options.daemonless:
                reason = labels.CONNECT_INTRO
                while True:
                    address = await self.push_screen_wait(
                        ConnectScreen(self.connection.address, reason=reason)
                    )
                    if address is None:
                        raise ConnectionError(labels.DAEMONLESS_CANCELLED)
                    try:
                        client = await self.connection.open(address=address)
                    except OSError:
                        reason = labels.CONNECT_RETRY
                        continue
                    break
            else:
                client = await self.connection.open(restart=self._restart)
                self._restart = False
        except Exception as exc:
            self.chat.presenter.error(labels.daemon_failed(exc))
            return

        self.chat.presenter.note(
            labels.daemon_connected(self.connection.address, self.connection.started)
        )
        try:
            self.state.config = await client.get_config()
        except Exception as exc:
            self.chat.presenter.error(labels.daemon_failed(exc))
        try:
            info = await self.chat.sessions.open()
        except Exception as exc:
            self.chat.presenter.error(labels.daemon_failed(exc))
            return
        self.chat._adopt_session(info)
        self.chat.presenter.note(labels.session_started(info.get("cwd", "")))
        if not self.state.has_credential:
            self.chat._open_settings(first_run=True)
        self.chat._load_info()

        # Close the iterator on cancellation while the event loop still exists.
        async with aclosing(client.events()) as frames:
            async for frame in frames:
                self.chat.handle_frame(frame)
