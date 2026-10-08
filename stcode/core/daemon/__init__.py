"""
Daemon — a long-lived process holding agents, speaking JSONL over a socket.

    async with Daemon(config) as daemon:
        await daemon.serve_forever()

The inversion the design rests on: a UI is a client, not the owner. Detaching leaves
the agent working, several clients may watch one session, and moving to TCP in a
container is a config key rather than a rewrite. The client lives in `stcode/cli/`
and shares nothing with this package but the wire format.

Five modules — `protocol` (the wire), `runner` (one session and its watchers), `server`
(socket and registry), `autonomy` (the `full-auto` guard), `main` (the entry point).
"""

from stcode.core.daemon.autonomy import AutonomyRefused, guard_autonomy, in_container
from stcode.core.daemon.protocol import ProtocolError, decode, encode, parse_client_message
from stcode.core.daemon.runner import SessionRunner
from stcode.core.daemon.server import Daemon, socket_path

__all__ = [
    "AutonomyRefused",
    "Daemon",
    "ProtocolError",
    "SessionRunner",
    "decode",
    "encode",
    "guard_autonomy",
    "in_container",
    "parse_client_message",
    "socket_path",
]
