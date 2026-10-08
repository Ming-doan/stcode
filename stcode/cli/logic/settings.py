"""Write defaults before session overrides; the daemon is authoritative."""

from __future__ import annotations

from typing import Any

from stcode.cli.models import ApprovalMode
from stcode.cli.services.client import DaemonClient
from .state import ClientState


class Settings:
    def __init__(self, state: ClientState) -> None:
        self.state = state

    async def set_config(self, client: DaemonClient, **fields: Any) -> None:
        self.state.config = await client.set_config(**fields)

    async def apply(self, client: DaemonClient, patch: dict[str, Any]) -> None:
        await self.set_config(client, **patch)
        if self.state.session_id:
            await client.set_meta(
                provider=str(self.state.config.get("provider", "")),
                model=str(self.state.config.get("model", "")),
            )

    async def set_mode(self, client: DaemonClient, mode: ApprovalMode) -> None:
        await self.set_config(client, approval_mode=mode)
        if self.state.session_id:
            await client.set_mode(mode)

    async def set_effort(self, client: DaemonClient, effort: str) -> None:
        await self.set_config(client, reasoning_effort=effort)
        if self.state.session_id:
            await client.set_meta(reasoning_effort=effort)
