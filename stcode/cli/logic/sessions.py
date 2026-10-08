"""Session operations preserve the distinction between local and daemon workspaces."""

from __future__ import annotations

from typing import Any

from stcode.cli.models import StartOptions
from stcode.cli.services.client import DaemonClient
from .connection import Connection
from .state import ClientState


class Sessions:
    def __init__(
        self, connection: Connection, state: ClientState, options: StartOptions
    ) -> None:
        self.connection = connection
        self.state = state
        self.options = options

    @property
    def client(self) -> DaemonClient:
        client = self.connection.client
        if client is None:
            raise ConnectionError("No daemon is connected")
        return client

    async def open(self) -> dict[str, Any]:
        if self.options.resume:
            return await self.client.attach(self.options.resume)
        cwd = (
            self.options.cwd
            if self.options.explicit_cwd or not self.options.daemonless
            else None
        )
        return await self.client.create(
            cwd=cwd, role=self.options.role, approval_mode=self.options.mode
        )

    async def list(self) -> list[dict[str, Any]]:
        return await self.client.sessions(50)

    async def detach(self) -> None:
        await self.client.detach(self.state.session_id)

    async def attach(self, session_id: str) -> dict[str, Any]:
        return await self.client.attach(session_id)

    async def clear(self) -> dict[str, Any]:
        previous = self.state.session_id
        frame = await self.client.create(
            cwd=self.options.cwd,
            role=self.options.role,
            approval_mode=self.options.mode,
        )
        if previous:
            await self.client.detach(previous)
        return frame

    async def load_info(self) -> None:
        self.state.info = await self.client.info()

    async def push(self, text: str) -> None:
        await self.client.push(text)

    async def interrupt(self) -> None:
        await self.client.interrupt()
