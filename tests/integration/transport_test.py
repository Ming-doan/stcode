"""Platform transport defaults must agree without requiring Unix-only asyncio APIs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from driving import asynctest

from stcode.cli.main import _address
from stcode.cli.services.client import Address, DaemonClient
from stcode.core.configs import Config, load_config
from stcode.core.daemon import Daemon


@pytest.fixture
def without_unix_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match Windows' asyncio surface while still using real TCP sockets."""
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)
    monkeypatch.delattr(asyncio, "start_unix_server", raising=False)


@pytest.mark.skipif(
    not hasattr(asyncio, "open_unix_connection")
    or not hasattr(asyncio, "start_unix_server"),
    reason="Unix socket APIs are unavailable",
)
def test_unix_remains_the_default_where_supported() -> None:
    assert Address().transport == "unix"
    assert _address(None, None, None, None).transport == "unix"
    assert Config().daemon.transport == "unix"
    assert _address("tcp", None, "127.0.0.1", 9000).daemon_args() == [
        "--transport", "tcp", "--host", "127.0.0.1", "--port", "9000",
    ]


@asynctest
async def test_defaults_connect_over_tcp_without_unix_apis(
    tmp_path: Path, without_unix_sockets: None,
) -> None:
    address = _address(None, None, None, None)
    assert address.transport == Address().transport == "tcp"
    assert str(address) == "127.0.0.1:7717"
    assert address.daemon_args() == [
        "--transport", "tcp", "--host", "127.0.0.1", "--port", "7717",
    ]

    path = tmp_path / "config.toml"
    config = load_config(
        path,
        create_if_missing=True,
        overrides={"session.dir": str(tmp_path / "sessions"), "daemon.port": 0},
    )
    assert config.daemon.transport == "tcp"
    assert load_config(path).daemon.transport == "tcp"
    assert config.daemon.host == address.host

    async with Daemon(config) as daemon:
        assert daemon._server is not None
        address.port = daemon._server.sockets[0].getsockname()[1]
        async with await DaemonClient.connect(address) as client:
            frame = await client.get_config()
            assert frame["path"] == str(path)
            assert frame["keys_editable"] is False
            assert await client.sessions() == []


@asynctest
async def test_explicit_unix_client_reports_how_to_use_tcp(
    tmp_path: Path, without_unix_sockets: None,
) -> None:
    address = _address("unix", str(tmp_path / "d.sock"), None, None)
    assert address.transport == "unix"
    with pytest.raises(OSError, match="Unix sockets.*--transport tcp"):
        await DaemonClient.connect(address)


@asynctest
async def test_explicit_unix_daemon_reports_how_to_use_tcp(
    tmp_path: Path, without_unix_sockets: None,
) -> None:
    config = load_config(
        tmp_path / "config.toml",
        create_if_missing=True,
        overrides={
            "daemon.transport": "unix",
            "daemon.socket": str(tmp_path / "d.sock"),
            "session.dir": str(tmp_path / "sessions"),
        },
    )
    assert config.daemon.transport == "unix"
    with pytest.raises(OSError, match="Unix sockets.*--transport tcp"):
        async with Daemon(config):
            pass
    assert not config.daemon.socket.exists()
