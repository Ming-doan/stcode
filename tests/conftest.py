"""
Shared scaffolding for every test.

Two things live here because nine test modules were each carrying their own copy:

* **Driving coroutines.** These tests do not use `pytest-asyncio`. A test either takes
  the `run` fixture (a module-scoped loop, for anything holding a subprocess or an MCP
  connection) or wears `@asynctest` (a fresh loop per test, for anything binding a
  socket). The two are not interchangeable — see each one's docstring. `asynctest`
  itself lives in `tests/driving.py`, because a test under
  `tests/integration/conftest.py` cannot reach this module by the name `conftest`.
* **The workspace.** `workspace` copies `tests/fixtures/workspace/` into `tmp_path` and
  hands back the root. Tests get a real project — config, `.mcp.json`, skills, a role,
  source files — without sharing mutable state with each other.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Coroutine, Iterator, TypeVar

import pytest
from driving import asynctest

__all__ = ["asynctest", "isolated_mcp_config", "loop", "run", "workspace"]

T = TypeVar("T")

FIXTURE_WORKSPACE = Path(__file__).parent / "fixtures" / "workspace"


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    """One event loop for the module.

    Subprocess transports bind to the loop that created them, so `asyncio.run()` per
    test leaves the REPL worker and any MCP server talking to a closed one.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def run(loop: asyncio.AbstractEventLoop) -> Any:
    """`run(coro)` — drive one coroutine to completion on the module's loop."""

    def _run(coro: Coroutine[Any, Any, T]) -> T:
        return loop.run_until_complete(coro)

    return _run




@pytest.fixture(autouse=True)
def isolated_mcp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own `~/.stcode/mcp.json` out of every test.

    The global config is merged under the project's, so without this a laptop with
    context7 configured runs a different suite from a laptop without one — and the test
    that asserts "no config, no servers" fails on the machine of whoever uses the
    feature. `USER_MCP_DIR` is computed from `Path.home()` at import time, so moving
    `$HOME` is too late; the attribute is what has to move.
    """
    from stcode.core.harness import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "USER_MCP_DIR", tmp_path / "no-such-home" / ".stcode")
    monkeypatch.delenv("STCODE_MCP_CONFIG", raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A copy of `tests/fixtures/workspace/`, as the agent's cwd.

    Copied rather than used in place: these tests write files, generate MCP stubs into
    `.stcode/mcp_servers/` and append sessions. A fixture directory the suite mutates
    is a fixture directory that makes the next run disagree with this one.
    """
    root = tmp_path / "workspace"
    shutil.copytree(FIXTURE_WORKSPACE, root)
    return root
