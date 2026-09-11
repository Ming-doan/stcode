"""
DaemonClient — the other end of the same JSONL.

    async with await DaemonClient.connect(config) as client:
        info = await client.create(cwd=Path.cwd())
        await client.push("Add rate limiting")
        async for frame in client.events():
            ...

Thin on purpose: it opens the transport, sends the eight client messages, and hands back
what the daemon says. It renders and interprets nothing — that would put display in
`core/`.

**Requests and events share one socket.** `create`, `attach` and `sessions` expect an
answer while deltas arrive, so one reader routes each frame to a waiter parked on that
frame's type, else to the event queue. Type is enough: a client has at most one of those
three in flight, and everything else is fire-and-forget or keyed by `execution_id`.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncIterator

from stcode.core.configs import GatewayConfig
from stcode.core.daemon.protocol import (
    Answer,
    Approval,
    Attach,
    Create,
    Detach,
    Interrupt,
    ProtocolError,
    Push,
    Sessions,
    SetMode,
    decode,
    encode,
)
from stcode.core.daemon.server import socket_path
from stcode.core.harness.approvals import ApprovalMode

REPLY_TIMEOUT = 30.0
"""How long a synchronous verb waits. Long enough for `create` to sample git and
connect MCP servers on a cold repo; short enough that a wedged daemon is reported
rather than hung on."""


class DaemonClosed(Exception):
    """The daemon went away while we were talking to it."""


class DaemonClient:
    """One connection to one daemon."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._events: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._waiters: dict[str, "asyncio.Future[dict[str, Any]]"] = {}
        self._closed = asyncio.Event()
        self._read_task = asyncio.create_task(self._read_loop(), name="stcode-client-reader")
        self.session_id: str = ""

    # ---- connecting ----

    @classmethod
    async def connect(cls, config: GatewayConfig) -> "DaemonClient":
        """Open the transport the config names. Raises `OSError` when nothing is
        listening — which is the caller's cue to start a daemon, not an error to hide."""
        settings = config.daemon
        if settings.transport == "unix":
            reader, writer = await asyncio.open_unix_connection(str(socket_path(settings.socket)))
        else:
            reader, writer = await asyncio.open_connection(settings.host, settings.port)
        return cls(reader, writer)

    # ---- io ----

    async def _read_loop(self) -> None:
        try:
            async for line in self._reader:
                if not line.strip():
                    continue
                try:
                    frame = decode(line)
                except ProtocolError:
                    continue  # A line we cannot parse is the daemon's bug, not a reason to drop.
                waiter = self._waiters.pop(str(frame.get("type", "")), None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame)
                else:
                    self._events.put_nowait(frame)
        finally:
            self._closed.set()
            for waiter in self._waiters.values():
                if not waiter.done():
                    waiter.set_exception(DaemonClosed("the daemon closed the connection"))
            self._waiters.clear()

    def _send(self, message: Any) -> None:
        self._writer.write(encode(message))

    async def _request(self, message: Any, expect: str) -> dict[str, Any]:
        """Send, then wait for the one frame type that answers it.

        An `error` frame resolves the wait too — a `create` that was refused must raise
        rather than sit until the timeout, because the refusal *is* the answer.
        """
        loop = asyncio.get_running_loop()
        reply: "asyncio.Future[dict[str, Any]]" = loop.create_future()
        error: "asyncio.Future[dict[str, Any]]" = loop.create_future()
        self._waiters[expect] = reply
        self._waiters["error"] = error
        try:
            self._send(message)
            await self._writer.drain()
            done, pending = await asyncio.wait(
                {reply, error}, timeout=REPLY_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            self._waiters.pop(expect, None)
            self._waiters.pop("error", None)

        # The loser of the race is cancelled rather than abandoned: `_read_loop` sets an
        # exception on every outstanding waiter when the socket closes, and a future
        # holding one nobody retrieves is a warning printed from the garbage collector.
        for future in pending:
            future.cancel()
        if not done:
            raise DaemonClosed(f"the daemon did not answer `{message.type}` in {REPLY_TIMEOUT:.0f}s")
        if error in done:
            frame = error.result()  # raises DaemonClosed if the socket died first
            raise DaemonClosed(str(frame.get("message", "the daemon reported an error")))
        return reply.result()

    async def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        """The next frame, for a caller that wants one rather than a stream — the
        `history` that follows an `attach`, say."""
        return await asyncio.wait_for(self._events.get(), timeout)

    def events(self) -> AsyncIterator[dict[str, Any]]:
        """Everything the daemon sends that was not an answer to a request."""

        async def stream() -> AsyncIterator[dict[str, Any]]:
            while True:
                getter = asyncio.ensure_future(self._events.get())
                closed = asyncio.ensure_future(self._closed.wait())
                done, _pending = await asyncio.wait(
                    {getter, closed}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    closed.cancel()
                    yield getter.result()
                    continue
                # Closed: drain whatever already arrived before stopping, so the last
                # `turn_finished` before a shutdown is not lost.
                getter.cancel()
                while not self._events.empty():
                    yield self._events.get_nowait()
                return

        return stream()

    # ---- the protocol ----

    async def create(
        self,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        approval_mode: ApprovalMode | None = None,
    ) -> dict[str, Any]:
        frame = await self._request(
            Create(cwd=str(cwd or ""), role=role, approval_mode=approval_mode), "session"
        )
        self.session_id = str(frame.get("id", ""))
        return frame

    async def attach(self, session_id: str, *, replay: bool = True) -> dict[str, Any]:
        frame = await self._request(Attach(session=session_id, replay=replay), "session")
        self.session_id = str(frame.get("id", ""))
        return frame

    async def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        frame = await self._request(Sessions(limit=limit), "sessions")
        rows = frame.get("sessions", [])
        return list(rows) if isinstance(rows, list) else []

    async def detach(self, session_id: str = "") -> None:
        await self._fire(Detach(session=session_id))

    async def push(self, text: str, session_id: str = "") -> None:
        await self._fire(Push(text=text, session=session_id))

    async def interrupt(self, session_id: str = "") -> None:
        await self._fire(Interrupt(session=session_id))

    async def approve(self, execution_id: str, approved: bool, session_id: str = "") -> None:
        await self._fire(Approval(execution_id=execution_id, approved=approved, session=session_id))

    async def answer(self, execution_id: str, text: str, session_id: str = "") -> None:
        await self._fire(Answer(execution_id=execution_id, text=text, session=session_id))

    async def set_mode(self, mode: ApprovalMode, session_id: str = "") -> None:
        await self._fire(SetMode(mode=mode, session=session_id))

    async def _fire(self, message: Any) -> None:
        self._send(message)
        await self._writer.drain()

    # ---- lifecycle ----

    async def aclose(self) -> None:
        self._read_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._read_task
        with contextlib.suppress(ConnectionError, OSError):
            self._writer.close()
            await self._writer.wait_closed()
        self._closed.set()

    async def __aenter__(self) -> "DaemonClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


__all__ = ["REPLY_TIMEOUT", "DaemonClient", "DaemonClosed"]
