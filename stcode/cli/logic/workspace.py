"""Workspace requests always target the attached daemon session."""

from typing import Any

from stcode.cli.models import UiPrefs
from stcode.cli.services.client import DaemonClient


class Workspace:
    def __init__(self, prefs: UiPrefs) -> None:
        self.prefs = prefs

    async def files(self, client: DaemonClient) -> list[str]:
        return await client.workspace_files()

    async def shell(self, client: DaemonClient, command: str) -> dict[str, Any]:
        return await client.workspace_shell(command, self.prefs.shell_timeout)
