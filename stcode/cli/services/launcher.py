"""
Launcher — find the daemon at an address, or start one there as a subprocess.

    launcher = Launcher(address)
    client = await launcher.connect()      # reuse a listening daemon, else start one
    ...
    await launcher.stop()                  # stops only a daemon this launcher started

The daemon is a separate program (`stcode-daemon`), reached only through its socket, so
this is the whole of what the UI knows about how one runs. Its output goes to
`daemon.log` beside the socket, and when it dies on start-up the tail of that log is
the error the user sees.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from stcode.cli.services.client import Address, DaemonClient
from stcode.cli.services.paths import stcode_home

START_TIMEOUT = 20.0
"""How long a fresh daemon gets to start listening. MCP-free start-up is well under a
second; the margin is for a cold Python import on a slow disk."""

STOP_TIMEOUT = 5.0


class DaemonFailed(Exception):
    """The daemon could not be started, or exited before it listened."""


def daemon_command() -> list[str]:
    """`stcode-daemon` from this environment when installed, else the module."""
    search = os.pathsep.join(
        [str(Path(sys.executable).parent), os.environ.get("PATH", "")]
    )
    found = shutil.which("stcode-daemon", path=search)
    return [found] if found else [sys.executable, "-m", "stcode.core"]


class Launcher:
    """Connects to the daemon at one address, starting it when nothing listens."""

    def __init__(
        self,
        address: Address,
        *,
        config: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.address = address
        self.config = config
        self.log_path = log_path or stcode_home() / "daemon.log"
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def started(self) -> bool:
        """Whether this launcher started the daemon it is connected to."""
        return self.process is not None

    async def connect(self, *, restart: bool = False) -> DaemonClient:
        """Reuse a listening daemon, or start one. `restart` stops a running one first."""
        if restart:
            await self.shutdown_running()
        try:
            return await DaemonClient.connect(self.address)
        except OSError:
            pass
        self._spawn()
        return await self._wait_until_listening()

    async def shutdown_running(self) -> None:
        """Stop whatever daemon is listening at the address, and wait until it is gone."""
        try:
            client = await DaemonClient.connect(self.address)
        except OSError:
            return
        async with client:
            await client.shutdown()
            if not await client.wait_closed(STOP_TIMEOUT):
                raise DaemonFailed(
                    f"the daemon at {self.address} did not stop (shutdown needs a unix socket)"
                )
        # The daemon closes clients before its listening socket. EOF alone can race
        # with connect() and hand the caller the very daemon it asked us to replace.
        deadline = asyncio.get_running_loop().time() + STOP_TIMEOUT
        while True:
            try:
                probe = await DaemonClient.connect(self.address)
            except OSError:
                return
            await probe.aclose()
            if asyncio.get_running_loop().time() >= deadline:
                raise DaemonFailed(
                    f"the daemon at {self.address} is still listening after shutdown"
                )
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        """Stop the daemon this launcher started. A reused one is left running."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()

    def _spawn(self) -> None:
        command = [*daemon_command(), *self.address.daemon_args()]
        if self.config is not None:
            command += ["--config", str(self.config)]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("wb") as log:
            # Its own session, so a ctrl-c meant for the UI is not also delivered here.
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

    async def _wait_until_listening(self) -> DaemonClient:
        assert self.process is not None
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while True:
            try:
                return await DaemonClient.connect(self.address)
            except OSError:
                pass
            if self.process.poll() is not None:
                self.process = None
                raise DaemonFailed(
                    self._log_tail() or "the daemon exited before it listened"
                )
            if asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise DaemonFailed(
                    f"the daemon did not listen on {self.address} in {START_TIMEOUT:.0f}s"
                )
            await asyncio.sleep(0.1)

    def _log_tail(self, lines: int = 5) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])


__all__ = ["DaemonFailed", "Launcher", "daemon_command"]
