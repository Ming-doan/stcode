"""Client-owned vocabulary and values; never imports the engine or terminal UI."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

ApprovalMode = Literal["plan", "suggest", "auto-edit", "full-auto"]
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("plan", "suggest", "auto-edit", "full-auto")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
Row = tuple[str, str]
ThemePreference = Literal["auto", "dark", "light"]
THEME_PREFERENCES: tuple[ThemePreference, ...] = ("auto", "dark", "light")
DEFAULT_THEME: ThemePreference = "auto"
DEFAULT_SHELL_TIMEOUT = 10.0
MAX_SHELL_TIMEOUT = 120.0


@dataclass(frozen=True)
class StartOptions:
    cwd: Path
    explicit_cwd: bool = False
    daemonless: bool = False
    resume: str = ""
    mode: ApprovalMode | None = None
    role: str = ""


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
