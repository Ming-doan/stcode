"""
Shared scaffolding for every test.

Two things live here because nine test modules were each carrying their own copy:

* **Driving coroutines.** These tests do not use `pytest-asyncio`. A test either takes
  the `run` fixture (a module-scoped loop, for anything holding a subprocess or an MCP
  connection) or wears `@asynctest` (a fresh loop per test, for anything binding a
  socket). The two are not interchangeable — see each one's docstring.
* **The workspace.** `workspace` copies `tests/fixtures/workspace/` into `tmp_path` and
  hands back the root. Tests get a real project — config, `.mcp.json`, skills, a role,
  source files — without sharing mutable state with each other.
"""

from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path
from typing import Any, Coroutine, Iterator, TypeVar

import pytest

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


def asynctest(fn: Any) -> Any:
    """Run an `async def` test on a **fresh** loop of its own.

    The opposite trade from `run`: tests that bind real sockets and start background
    tasks must not share a loop, or one daemon's server outlives its test and is
    still listening when the next one starts. `functools.wraps` keeps the signature
    pytest introspects, so fixtures work unchanged.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(fn(*args, **kwargs))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    return wrapper


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
