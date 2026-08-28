"""
Message Pump — one reader per channel, fanning messages out by `parent_header.msg_id`.

Two things make this necessary rather than reading channels inline in `execute()`:

1. **Nothing may be dropped between executions.** A per-execute read loop stops reading
   the moment it sees `status: idle`, so a late `stream` message, or a stale reply from
   a cell we timed out on, lands in the *next* cell's result. The pump drains
   continuously and routes by parent, so a message can only ever reach the execution
   that caused it.
2. **A message can arrive before we subscribe.** `client.execute()` hands back the
   msg_id only after sending, and the kernel's `busy`/`execute_input` can already be in
   flight. Queues are therefore created on first sight of a parent id, not on subscribe
   — `subscribe()` adopts whatever has already accumulated.

`shell` and `iopub` share one routing table: `execute()` needs both the iopub `idle` and
the shell `execute_reply` to know a cell is done, and consuming them from a single queue
removes the race between the two.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Iterator

from jupyter_client.asynchronous.client import AsyncKernelClient

Channel = str
KernelMessage = tuple[Channel, dict[str, Any]]

_MAX_ORPHANS = 32
"""Cap on parent ids the pump has seen but nobody has subscribed to. Bounds the memory a
misbehaving or foreign client could cost us; the oldest is evicted first."""


class MessagePump:
    """Drains a kernel client's shell and iopub channels into per-execution queues."""

    def __init__(self, client: AsyncKernelClient) -> None:
        self._client = client
        self._queues: OrderedDict[str, asyncio.Queue[KernelMessage]] = OrderedDict()
        self._subscribed: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._drain("iopub", self._client.get_iopub_msg), name="pump-iopub"),
            asyncio.create_task(self._drain("shell", self._client.get_shell_msg), name="pump-shell"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except BaseException:  # teardown is best-effort — cancellation included
                pass
        self._tasks = []
        self._queues.clear()
        self._subscribed.clear()

    @contextmanager
    def subscribe(self, msg_id: str) -> Iterator[asyncio.Queue[KernelMessage]]:
        """Claim the queue for one execution, adopting anything already buffered for it."""
        queue = self._queue_for(msg_id)
        self._subscribed.add(msg_id)
        try:
            yield queue
        finally:
            self._subscribed.discard(msg_id)
            self._queues.pop(msg_id, None)

    def _queue_for(self, msg_id: str) -> asyncio.Queue[KernelMessage]:
        queue = self._queues.get(msg_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[msg_id] = queue
            self._evict_orphans()
        return queue

    def _evict_orphans(self) -> None:
        orphans = [k for k in self._queues if k not in self._subscribed]
        for msg_id in orphans[: max(len(orphans) - _MAX_ORPHANS, 0)]:
            self._queues.pop(msg_id, None)

    async def _drain(self, channel: Channel, receive: Any) -> None:
        while True:
            try:
                message = await receive()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Channel closed under us (shutdown, restart). The owner restarts the
                # pump after re-establishing channels; nothing to recover here.
                return
            parent = message.get("parent_header", {}).get("msg_id")
            if not parent:
                continue
            self._queue_for(parent).put_nowait((channel, message))
