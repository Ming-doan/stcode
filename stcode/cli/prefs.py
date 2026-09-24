"""
UI preferences — `~/.stcode/ui.toml`, and nothing else reads it.

Two keys: the theme, and the folders you have trusted. Both are facts about *this
terminal on this machine*, which is exactly why they are not in `config.toml` — that
file is read by the daemon, including one inside a container, and a container has no
theme and trusts nothing. → `docs/decisions/0003-what-the-tui-owns.md`

Flat rather than `[ui]`-sectioned: the file *is* the UI's, so a section header would be
the same word twice.

It sits beside whichever config file is in force, so `STCODE_CONFIG` takes the
preferences with it. It is deliberately **not** moved by `--config`: that flag chooses
an agent configuration for one run, and one run should not be able to forget which
folders you trust.
"""

from __future__ import annotations

from stcode.core.common.compat import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

from stcode.core.common.paths import config_dir

UI_FILENAME = "ui.toml"

ThemePreference = Literal["auto", "dark", "light"]
THEME_PREFERENCES: tuple[ThemePreference, ...] = ("auto", "dark", "light")
DEFAULT_THEME: ThemePreference = "auto"

DEFAULT_SHELL_TIMEOUT = 10.0
MAX_SHELL_TIMEOUT = 120.0
"""How long a `!` command may run, and the ceiling on raising it.

Ten seconds covers `git status`, `ls`, `docker ps` — the things `!` is for. The ceiling
is there because this runs on the **UI's** event loop budget, not the agent's: a command
that needs two minutes is a command that belongs in a second terminal, or in a `bash`
tool call where the agent can watch it.
"""


def clamp_shell_timeout(value: float) -> float:
    """Keep a hand-edited `shell_timeout` inside the range the UI can honour."""
    return max(1.0, min(float(value), MAX_SHELL_TIMEOUT))


def ui_path() -> Path:
    return config_dir() / UI_FILENAME


class UiPrefs(BaseModel):
    """What the terminal remembers between runs."""

    theme: ThemePreference = DEFAULT_THEME
    trusted: list[str] = Field(default_factory=list)
    """Absolute paths the user has approved the agent working in. A folder's children
    are covered by it — a project has subdirectories, and asking again for each one
    trains people to click through the question."""

    shell_timeout: float = DEFAULT_SHELL_TIMEOUT
    """Seconds a `!` command may run before it is killed. Capped at
    `MAX_SHELL_TIMEOUT`. Here rather than in `config.toml` because `!` runs on *this*
    terminal's machine and the agent never sees it — same split as the theme."""

    def is_trusted(self, path: str | Path) -> bool:
        """Whether `path`, or a parent of it, has been trusted.

        Compared as paths, not as strings: `startswith` would let `/home/me/work`
        trust `/home/me/work-other`, which is a different project.
        """
        target = Path(path).expanduser().resolve()
        for entry in self.trusted:
            root = Path(entry).expanduser()
            if target == root or root in target.parents:
                return True
        return False

    def trust(self, path: str | Path) -> None:
        """Approve `path`. Idempotent, and stored resolved so `.` never gets in."""
        resolved = str(Path(path).expanduser().resolve())
        if resolved not in self.trusted:
            self.trusted.append(resolved)


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
