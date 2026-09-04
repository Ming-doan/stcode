"""
Daemon — a long-lived process holding agents, speaking JSONL over a socket.

    async with Daemon(config) as daemon:
        await daemon.serve_forever()

    async with await DaemonClient.connect(config) as client:
        await client.create(cwd=Path.cwd())
        await client.push("Add rate limiting to the API")
        async for frame in client.events():
            ...

The inversion the whole design rests on: the TUI is a client, not the owner. Detaching
leaves the agent working, several clients may watch one session at once, and moving
from a unix socket to TCP in a container is a config key rather than a rewrite.

Four modules, one job each — `protocol` (the wire), `runner` (one live session and its
watchers), `server` (the socket and the registry), `autonomy` (invariant 5, as code).
"""

from stcode.core.daemon.autonomy import AutonomyRefused, guard_autonomy, in_container
from stcode.core.daemon.client import DaemonClient, DaemonClosed
from stcode.core.daemon.protocol import ProtocolError, decode, encode, parse_client_message
from stcode.core.daemon.runner import SessionRunner
from stcode.core.daemon.server import Daemon, socket_path

__all__ = [
    "AutonomyRefused",
    "Daemon",
    "DaemonClient",
    "DaemonClosed",
    "ProtocolError",
    "SessionRunner",
    "decode",
    "encode",
    "guard_autonomy",
    "in_container",
    "parse_client_message",
    "socket_path",
]
