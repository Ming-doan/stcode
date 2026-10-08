"""
UI preferences — `~/.stcode/ui.toml`, and nothing else reads it.

Two keys: the theme, and the folders you have trusted. Both are facts about *this
terminal on this machine*, which is exactly why they are not in `config.toml` — that
file is read by the daemon, including one inside a container, and a container has no
theme and trusts nothing. → `docs/decisions/0003-what-the-tui-owns.md`

Flat rather than `[ui]`-sectioned: the file *is* the UI's, so a section header would be
the same word twice.

It lives in the stcode home (`STCODE_HOME`, default `~/.stcode`). It is deliberately
**not** moved by `--config`: that flag chooses an agent configuration for one run, and
one run should not be able to forget which folders you trust.
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomli_w
from stcode.cli.models import (
    UiPrefs,
    ThemePreference,
    THEME_PREFERENCES,
    DEFAULT_THEME,
    DEFAULT_SHELL_TIMEOUT,
    MAX_SHELL_TIMEOUT,
)

from stcode.cli.services.paths import stcode_home

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the 3.10 CI job
    import tomli as tomllib

UI_FILENAME = "ui.toml"


def clamp_shell_timeout(value: float) -> float:
    """Keep a hand-edited `shell_timeout` inside the range the UI can honour."""
    return max(1.0, min(float(value), MAX_SHELL_TIMEOUT))


def ui_path() -> Path:
    return stcode_home() / UI_FILENAME


def load_prefs(path: Path | None = None) -> UiPrefs:
    """Read the preferences, falling back to the defaults for anything unreadable.

    **Fails closed and never raises.** A file somebody hand-edited badly must not be
    the reason stcode will not start — and must not be the reason it runs somewhere
    nobody approved, so a broken `trusted` list reads as an empty one rather than as a
    permissive one.
    """
    path = path or ui_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return UiPrefs()
    if not isinstance(data, dict):
        return UiPrefs()

    theme = data.get("theme")
    trusted = data.get("trusted")
    timeout = data.get("shell_timeout")
    return UiPrefs(
        # Per key, not whole-file: an unknown theme name is no reason to forget the
        # trusted list sitting two lines below it.
        theme=theme if theme in THEME_PREFERENCES else DEFAULT_THEME,
        trusted=[str(entry) for entry in trusted if isinstance(entry, str)]
        if isinstance(trusted, list)
        else [],
        shell_timeout=clamp_shell_timeout(timeout)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        else DEFAULT_SHELL_TIMEOUT,
    )


def save_prefs(prefs: UiPrefs, path: Path | None = None) -> Path:
    """Write them out, atomically. No secrets in here, so no 0600 dance — but an
    interrupted write must not leave a file that reads as "nothing is trusted"."""
    path = path or ui_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(tomli_w.dumps(prefs.model_dump(mode="json")))
    tmp.replace(path)
    return path


__all__ = [
    "DEFAULT_SHELL_TIMEOUT",
    "DEFAULT_THEME",
    "MAX_SHELL_TIMEOUT",
    "THEME_PREFERENCES",
    "UI_FILENAME",
    "ThemePreference",
    "UiPrefs",
    "clamp_shell_timeout",
    "load_prefs",
    "save_prefs",
    "ui_path",
]
