"""FIFO requests keep parallel approvals and questions individually answerable."""

from collections import deque
from typing import Any


class RequestQueue:
    def __init__(self) -> None:
        self._pending: deque[dict[str, Any]] = deque()

    def append(self, frame: dict[str, Any]) -> None:
        self._pending.append(frame)

    def pop(self) -> dict[str, Any] | None:
        return self._pending.popleft() if self._pending else None

    def clear(self) -> None:
        self._pending.clear()
