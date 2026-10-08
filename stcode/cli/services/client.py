"""
DaemonClient — the UI's end of the daemon's JSONL socket.

    async with await DaemonClient.connect(Address()) as client:
        await client.create(cwd=Path.cwd())
        await client.push("Add rate limiting")
        async for frame in client.events():
            ...

Shares nothing with `stcode/core` but the wire format: messages go out as dicts and
come back as dicts. It renders and interprets nothing.

**Requests and events share one socket.** `create`, `attach`, `sessions`, `info` and
the config verbs expect an answer while deltas arrive, so one reader routes each frame
to a waiter parked on that frame's type, else to the event queue. Type is enough: a
client has at most one of those in flight, and everything else is fire-and-forget or
keyed by `execution_id`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncIterator

from stcode.cli.services.paths import stcode_home

STREAM_LIMIT = 64 * 1024 * 1024
"""Longest line either end accepts. `history` is one line carrying a whole transcript,
and asyncio's 64 KiB default would make a session unresumable after ~30 tool calls."""

REPLY_TIMEOUT = 30.0
"""How long a synchronous verb waits. Long enough for `create` to sample git and
connect MCP servers on a cold repo; short enough that a wedged daemon is reported
rather than hung on."""


@dataclass
class Address:
    """Where a daemon listens."""

    transport: str = "unix"
    socket: Path = field(default_factory=lambda: stcode_home() / "daemon.sock")
    host: str = "127.0.0.1"
    port: int = 7717

    def __str__(self) -> str:
        return (
            str(self.socket) if self.transport == "unix" else f"{self.host}:{self.port}"
        )

    def daemon_args(self) -> list[str]:
        """The same address as `stcode-daemon` flags."""
        if self.transport == "unix":
            return ["--transport", "unix", "--socket", str(self.socket)]
        return ["--transport", "tcp", "--host", self.host, "--port", str(self.port)]


class DaemonClosed(Exception):
    """The daemon went away, refused a request, or did not answer it."""


class DaemonClient:
    """One connection to one daemon."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._events: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._waiters: dict[str, "asyncio.Future[dict[str, Any]]"] = {}
        self._closed = asyncio.Event()
        self._read_task = asyncio.create_task(
            self._read_loop(), name="stcode-client-reader"
        )
        self.session_id: str = ""

    @classmethod
    async def connect(cls, address: Address) -> "DaemonClient":
        """Open the transport. `OSError` when nothing is listening — the caller's cue to
        start a daemon or ask for another address, not an error to hide."""
        if address.transport == "unix":
            reader, writer = await asyncio.open_unix_connection(
                str(address.socket.expanduser()), limit=STREAM_LIMIT
            )
        else:
            reader, writer = await asyncio.open_connection(
                address.host, address.port, limit=STREAM_LIMIT
            )
        return cls(reader, writer)

    # ---- io ----

    async def _read_loop(self) -> None:
        try:
            async for line in self._reader:
                if not line.strip():
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A line we cannot parse is the daemon's bug, not a reason to drop.
                if not isinstance(frame, dict):
                    continue
                waiter = self._waiters.pop(str(frame.get("type", "")), None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame)
                else:
                    self._events.put_nowait(frame)
        except ValueError as exc:
            # A line longer than `STREAM_LIMIT`: the framing cannot be recovered, so the
            # connection ends — saying so.
            self._events.put_nowait(
                {
                    "type": "error",
                    "message": f"frame too large for the connection: {exc}",
                }
            )
        finally:
            self._closed.set()
            for waiter in self._waiters.values():
                if not waiter.done():
                    waiter.set_exception(
                        DaemonClosed("the daemon closed the connection")
                    )
            self._waiters.clear()

    async def _fire(self, kind: str, **fields: Any) -> None:
        message = {
            "type": kind,
            **{key: value for key, value in fields.items() if value is not None},
        }
        self._writer.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await self._writer.drain()

    async def _request(self, kind: str, expect: str, **fields: Any) -> dict[str, Any]:
        """Send, then wait for the one frame type that answers it.

        An `error` frame resolves the wait too — a refused request must raise rather
        than sit until the timeout, because the refusal *is* the answer.
        """
        loop = asyncio.get_running_loop()
        reply: "asyncio.Future[dict[str, Any]]" = loop.create_future()
        error: "asyncio.Future[dict[str, Any]]" = loop.create_future()
        self._waiters[expect] = reply
        self._waiters["error"] = error
        try:
            await self._fire(kind, **fields)
            done, pending = await asyncio.wait(
                {reply, error},
                timeout=REPLY_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._waiters.pop(expect, None)
            self._waiters.pop("error", None)
        # Cancel the loser: a future holding an exception nobody retrieves is a warning
        # printed from the garbage collector.
        for future in pending:
            future.cancel()
        if not done:
            raise DaemonClosed(
                f"the daemon did not answer `{kind}` in {REPLY_TIMEOUT:.0f}s"
            )
        if error in done:
            frame = error.result()  # raises DaemonClosed if the socket died first
            raise DaemonClosed(
                str(frame.get("message", "the daemon reported an error"))
            )
        return reply.result()

    async def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        """The next frame, for a caller that wants one rather than a stream."""
        return await asyncio.wait_for(self._events.get(), timeout)

    def events(self) -> AsyncIterator[dict[str, Any]]:
        """Everything the daemon sends that was not an answer to a request.

        Waits on the next frame and the socket closing at once, and cancels both in
        `finally`: a task left parked there is finalised by the garbage collector,
        possibly after the loop has closed.
        """

        async def stream() -> AsyncIterator[dict[str, Any]]:
            pending: set["asyncio.Future[Any]"] = set()
            try:
                while True:
                    getter = asyncio.ensure_future(self._events.get())
                    closed = asyncio.ensure_future(self._closed.wait())
                    pending = {getter, closed}
                    done, _ = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    if getter in done:
                        closed.cancel()
                        pending = set()
                        yield getter.result()
                        continue
                    # Closed: drain what already arrived, so the last frame is not lost.
                    getter.cancel()
                    pending = set()
                    while not self._events.empty():
                        yield self._events.get_nowait()
                    return
            finally:
                for future in pending:
                    if not future.done():
                        future.cancel()

        return stream()

    async def wait_closed(self, timeout: float) -> bool:
        """Whether the daemon closed this connection within `timeout` seconds."""
        try:
            await asyncio.wait_for(self._closed.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # ---- the protocol ----

    async def create(
        self,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        approval_mode: str | None = None,
    ) -> dict[str, Any]:
        frame = await self._request(
            "create",
            "session",
            cwd=str(cwd or ""),
            role=role,
            approval_mode=approval_mode,
        )
        self.session_id = str(frame.get("id", ""))
        return frame

    async def attach(self, session_id: str, *, replay: bool = True) -> dict[str, Any]:
        frame = await self._request(
            "attach", "session", session=session_id, replay=replay
        )
        self.session_id = str(frame.get("id", ""))
        return frame

    async def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        frame = await self._request("sessions", "sessions", limit=limit)
        rows = frame.get("sessions", [])
        return list(rows) if isinstance(rows, list) else []

    async def detach(self, session_id: str = "") -> None:
        await self._fire("detach", session=session_id)

    async def push(self, text: str, session_id: str = "") -> None:
        await self._fire("push", text=text, session=session_id)

    async def interrupt(self, session_id: str = "") -> None:
        await self._fire("interrupt", session=session_id)

    async def approve(
        self, execution_id: str, approved: bool, session_id: str = ""
    ) -> None:
        await self._fire(
            "approval", execution_id=execution_id, approved=approved, session=session_id
        )

    async def answer(self, execution_id: str, text: str, session_id: str = "") -> None:
        await self._fire(
            "answer", execution_id=execution_id, text=text, session=session_id
        )

    async def set_mode(self, mode: str, session_id: str = "") -> None:
        """Answered by a `session` frame with the mode now in force."""
        await self._fire("set_mode", mode=mode, session=session_id)

    async def set_meta(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        reasoning_effort: str | None = None,
        session_id: str = "",
    ) -> None:
        """Override this session's model, provider or effort. Answered by a `session`
        frame saying what the settings now are."""
        await self._fire(
            "set_meta",
            model=model or None,
            provider=provider or None,
            reasoning_effort=reasoning_effort or None,
            session=session_id,
        )

    async def info(self, session_id: str = "") -> dict[str, Any]:
        """Skills, MCP servers, tools and paths — as the **daemon's** machine sees them."""
        return await self._request("info", "info", session=session_id)

    async def get_config(self) -> dict[str, Any]:
        """The daemon's `config` frame: default provider and model, effort, mode, the
        configured providers (without keys) and the libraries a new one can use."""
        return await self._request("get_config", "config")

    async def set_config(self, **fields: Any) -> dict[str, Any]:
        """Persist `provider`, `model`, `api_key`, `reasoning_effort` or `approval_mode`
        in the daemon's config file. Raises `DaemonClosed` with the reason if refused."""
        return await self._request("set_config", "config", **fields)

    async def shutdown(self) -> None:
        """Ask the daemon to stop. Honoured over a unix socket only."""
        await self._fire("shutdown")

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


__all__ = [
    "REPLY_TIMEOUT",
    "STREAM_LIMIT",
    "Address",
    "DaemonClient",
    "DaemonClosed",
    "stcode_home",
]
