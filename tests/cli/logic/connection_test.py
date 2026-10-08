"""Connection replacement releases old sockets and retains launcher ownership."""

import asyncio

from stcode.cli.logic.connection import Connection


def test_reconnecting_closes_the_old_socket_before_opening_another() -> None:
    calls: list[str] = []

    class Client:
        async def aclose(self) -> None:
            calls.append("close")

    class Launcher:
        async def connect(self, *, restart: bool) -> Client:
            calls.append("restart" if restart else "connect")
            return Client()

        async def stop(self) -> None:
            calls.append("stop-owned-daemon")

    async def scenario() -> None:
        connection = Connection(Launcher())
        await connection.open()
        await connection.open(restart=True)
        await connection.aclose()
        assert connection.client is None

    asyncio.run(scenario())
    assert calls == ["connect", "close", "restart", "close", "stop-owned-daemon"]
