"""Construct client dependencies before handing the terminal to Textual."""

from __future__ import annotations

import contextlib
from pathlib import Path

from stcode.cli.logic.connection import Connection
from stcode.cli.logic.state import ClientState
from stcode.cli.models import ApprovalMode, StartOptions
from stcode.cli.services.launcher import Launcher
from stcode.cli.services.preferences import load_prefs, save_prefs, ui_path
from stcode.cli.ui.app import StcodeApp
from stcode.cli.ui.theme import TerminalMode, detect_terminal_mode


def build_app(
    launcher: Launcher,
    *,
    daemonless: bool = False,
    restart: bool = False,
    cwd: Path | None = None,
    resume: str = "",
    mode: ApprovalMode | None = None,
    role: str = "",
    terminal_mode: TerminalMode = "dark",
    prefs_path: Path | None = None,
) -> StcodeApp:
    path = prefs_path or ui_path()
    prefs = load_prefs(path)

    def save_preferences() -> None:
        with contextlib.suppress(OSError):
            save_prefs(prefs, path)

    return StcodeApp(
        Connection(launcher),
        ClientState(),
        StartOptions(
            cwd or Path.cwd(), cwd is not None, daemonless, resume, mode, role
        ),
        prefs,
        path,
        save_preferences=save_preferences,
        restart=restart,
        terminal_mode=terminal_mode,
    )


def run(
    launcher: Launcher,
    *,
    daemonless: bool = False,
    restart: bool = False,
    cwd: Path | None = None,
    resume: str = "",
    mode: ApprovalMode | None = None,
    role: str = "",
) -> None:
    # Detection must precede Textual taking ownership of the tty.
    build_app(
        launcher,
        daemonless=daemonless,
        restart=restart,
        cwd=cwd,
        resume=resume,
        mode=mode,
        role=role,
        terminal_mode=detect_terminal_mode(),
    ).run()
