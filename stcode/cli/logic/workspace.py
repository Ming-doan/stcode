"""Select the correct workspace for local client operations."""

from pathlib import Path

from stcode.cli.models import StartOptions, UiPrefs
from stcode.cli.services.files import list_files
from stcode.cli.services.shell import ShellResult, run_shell
from .state import ClientState


class Workspace:
    def __init__(
        self, options: StartOptions, state: ClientState, prefs: UiPrefs
    ) -> None:
        self.options, self.state, self.prefs = options, state, prefs

    def files(self) -> list[str]:
        root = Path(str(self.state.info.get("cwd", "") or self.options.cwd))
        return list_files(root) if root.is_dir() else []

    def shell(self, command: str) -> ShellResult:
        return run_shell(command, self.options.cwd, self.prefs.shell_timeout)
