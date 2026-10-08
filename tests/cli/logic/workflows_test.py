"""Client state changes only after successful daemon replies."""

import asyncio
from typing import Any

import pytest

from stcode.cli.logic.commands import parse_command, skill_names
from stcode.cli.logic.requests import RequestQueue
from stcode.cli.logic.settings import Settings
from stcode.cli.logic.state import ClientState


def test_command_arguments_and_registry_skill_case_are_preserved() -> None:
    assert parse_command("/MoDe  auto-edit ") == ("mode", "auto-edit")
    assert parse_command("/PDF Read MyFile.py") == ("pdf", "Read MyFile.py")
    assert skill_names({"skills": [{"name": "PDF"}, {"name": "pdf"}]}) == {"pdf": "PDF"}


def test_parallel_questions_keep_their_execution_ids_and_order() -> None:
    queue = RequestQueue()
    queue.append({"type": "approval_request", "execution_id": "first"})
    queue.append({"type": "question", "execution_id": "second"})
    assert queue.pop()["execution_id"] == "first"
    assert queue.pop()["execution_id"] == "second"
    assert queue.pop() is None
    queue.append({"execution_id": "old-session"})
    queue.clear()
    assert queue.pop() is None


def test_refused_default_does_not_change_the_snapshot_or_live_session() -> None:
    class RefusingClient:
        async def set_config(self, **fields: Any) -> dict[str, Any]:
            raise ValueError("full-auto refused")

        async def set_mode(self, mode: str) -> None:
            pytest.fail("the live session changed after the default was refused")

    state = ClientState(session_id="session", config={"approval_mode": "suggest"})
    settings = Settings(state)
    with pytest.raises(ValueError, match="refused"):
        asyncio.run(settings.set_mode(RefusingClient(), "full-auto"))
    assert state.config == {"approval_mode": "suggest"}


def test_mode_waits_for_the_session_frame_before_becoming_effective() -> None:
    calls: list[object] = []

    class AcceptingClient:
        async def set_config(self, **fields: Any) -> dict[str, Any]:
            calls.append(fields)
            return {"approval_mode": "auto-edit"}

        async def set_mode(self, mode: str) -> None:
            calls.append(mode)

    state = ClientState(session_id="session", effective={"mode": "suggest"})
    asyncio.run(Settings(state).set_mode(AcceptingClient(), "auto-edit"))
    assert calls == [{"approval_mode": "auto-edit"}, "auto-edit"]
    assert state.effective["mode"] == "suggest"
    state.adopt_session({"id": "session", "approval_mode": "auto-edit"})
    assert state.effective["mode"] == "auto-edit"
