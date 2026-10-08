"""Connection ownership and cleanup; interaction with the user stays in the UI."""

from __future__ import annotations

import contextlib

from stcode.cli.services.client import Address, DaemonClient
from stcode.cli.services.launcher import Launcher


class Connection:
    def __init__(self, launcher: Launcher) -> None:
        self.launcher = launcher
        self.client: DaemonClient | None = None

    @property
    def address(self) -> Address:
        return self.launcher.address

    @property
    def started(self) -> bool:
        return self.launcher.started

    async def close_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def open(
        self, *, address: Address | None = None, restart: bool = False
    ) -> DaemonClient:
        await self.close_client()
        if address is None:
            client = await self.launcher.connect(restart=restart)
        else:
            client = await DaemonClient.connect(address)
            self.launcher.address = address
        self.client = client
        return client

    async def aclose(self) -> None:
        try:
            await self.close_client()
        finally:
            await self.launcher.stop()
