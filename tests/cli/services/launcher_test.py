"""
Launcher tests — a real `stcode-daemon` subprocess, found or started, then stopped.

The UI reaches the daemon only through its socket and its exit status, so that is all
these use: no import of `stcode.core`, which is the boundary under test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from stcode.cli.services.client import Address
from stcode.cli.services.launcher import DaemonFailed, Launcher


def test_shutdown_waits_for_the_listener_after_the_client_reaches_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF can precede listener shutdown; restarting must not reuse that listener."""
    from stcode.cli.services import launcher as module

    calls: list[str] = []

    class ClosingClient:
        async def __aenter__(self) -> "ClosingClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.aclose()

        async def shutdown(self) -> None:
            calls.append("shutdown")

        async def wait_closed(self, timeout: float) -> bool:
            return True

        async def aclose(self) -> None:
            calls.append("close")

    async def connect(address: Address) -> ClosingClient:
        calls.append("connect")
        if calls.count("connect") == 3:
            raise ConnectionRefusedError()
        return ClosingClient()

    monkeypatch.setattr(module.DaemonClient, "connect", connect)
    asyncio.run(Launcher(Address()).shutdown_running())
    assert calls.count("connect") == 3
    assert calls.count("close") == 2


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private stcode home, inherited by the daemon the launcher starts."""
    monkeypatch.setenv("STCODE_HOME", str(tmp_path))
    return tmp_path


def launcher(home: Path, config: Path | None = None) -> Launcher:
    return Launcher(
        Address(socket=home / "d.sock"), config=config, log_path=home / "daemon.log"
    )


def test_a_daemon_is_started_when_none_listens_and_stopped_on_exit(home: Path) -> None:
    async def scenario() -> None:
        first = launcher(home)
        client = await first.connect()
        assert first.started
        frame = await client.get_config()
        assert frame["path"] == str(home / "config.toml"), (
            "the daemon scaffolded its own config"
        )
        await client.aclose()
        await first.stop()
        assert not (home / "d.sock").exists(), "the daemon did not clean up its socket"

    asyncio.run(scenario())


def test_a_listening_daemon_is_reused_and_left_running(home: Path) -> None:
    """Quitting a UI that found a daemon is a detach — someone else's agent keeps working."""

    async def scenario() -> None:
        owner = launcher(home)
        owner_client = await owner.connect()
        guest = launcher(home)
        client = await guest.connect()
        assert not guest.started
        await client.aclose()
        await guest.stop()
        assert (home / "d.sock").exists(), "a reused daemon was stopped"
        await owner_client.aclose()
        await owner.stop()

    asyncio.run(scenario())


def test_restart_replaces_a_running_daemon(home: Path) -> None:
    async def scenario() -> None:
        old = launcher(home)
        await old.connect()
        old_pid = old.process.pid  # type: ignore[union-attr]
        new = launcher(home)
        client = await new.connect(restart=True)
        assert new.started and new.process.pid != old_pid  # type: ignore[union-attr]
        assert old.process.wait(5) == 0, "the old daemon did not exit cleanly"  # type: ignore[union-attr]
        await client.aclose()
        await new.stop()

    asyncio.run(scenario())


def test_a_daemon_that_cannot_start_says_why(home: Path) -> None:
    """The log tail is the error: "connection refused" would send someone looking at
    sockets when the problem is a line in their config."""
    config = home / "broken.toml"
    config.write_text('[model]\ndefault = "ghost"\n')

    async def scenario() -> None:
        with pytest.raises(DaemonFailed, match="ghost"):
            await launcher(home, config).connect()

    asyncio.run(scenario())
