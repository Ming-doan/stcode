"""Remote sessions must not silently adopt the terminal's working directory."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stcode.cli.logic.sessions import Sessions
from stcode.cli.logic.state import ClientState
from stcode.cli.models import StartOptions


@pytest.mark.parametrize(
    "daemonless,explicit", [(True, False), (True, True), (False, False)]
)
def test_only_an_explicit_remote_workspace_overrides_the_daemon(
    tmp_path: Path,
    daemonless: bool,
    explicit: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        async def create(self, **fields: Any) -> dict[str, Any]:
            calls.append(fields)
            return {"id": "created"}

    options = StartOptions(
        tmp_path, explicit_cwd=explicit, daemonless=daemonless, mode="plan", role="dev"
    )
    session = Sessions(SimpleNamespace(client=Client()), ClientState(), options)
    assert asyncio.run(session.open()) == {"id": "created"}
    assert calls == [
        {
            "cwd": tmp_path if explicit or not daemonless else None,
            "approval_mode": "plan",
            "role": "dev",
        }
    ]


def test_resuming_attaches_without_creating_a_replacement(tmp_path: Path) -> None:
    class Client:
        async def attach(self, session_id: str) -> dict[str, Any]:
            return {"id": session_id}

        async def create(self, **fields: Any) -> dict[str, Any]:
            pytest.fail("resume must not create a new session")

    session = Sessions(
        SimpleNamespace(client=Client()),
        ClientState(),
        StartOptions(tmp_path, resume="saved"),
    )
    assert asyncio.run(session.open()) == {"id": "saved"}
