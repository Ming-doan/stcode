"""
Driving coroutines — the two ways a test runs an `async def`, and the trade between them.

Here rather than in `tests/conftest.py` because `tests/integration/conftest.py` is also
importable as `conftest`, so a test under it cannot reach the root one by that name.
Both conftests re-export what they need from here; nine modules still say
`from conftest import asynctest` and always will.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any


def asynctest(fn: Any) -> Any:
    """Run an `async def` test on a **fresh** loop of its own.

    The opposite trade from the module-scoped `loop` fixture: tests that bind real
    sockets and start background tasks must not share a loop, or one daemon's server
    outlives its test and is still listening when the next one starts.
    `functools.wraps` keeps the signature pytest introspects, so fixtures work
    unchanged.
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


__all__ = ["asynctest"]
