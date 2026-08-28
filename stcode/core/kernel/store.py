"""
Tool Output Store — interface for `tool_out`, ahead of the layer that fills it.

CLAUDE.md §9 asks for a GC policy from day one: spill above a size threshold, LRU-evict,
`del` what nothing references. The policy belongs here, but nothing writes tool results
until `core/harness/` exists, so this lands as the shape those tools will code against
rather than an eviction loop with no data to evict.

**Not yet wired into the kernel.** `bootstrap.py` injects a plain `dict` for `tool_out`;
swapping it for this store is the last step of the harness work, and needs the store to
be constructible *inside* the kernel process.
"""

from __future__ import annotations

from typing import Any, Iterator, MutableMapping

DEFAULT_SPILL_THRESHOLD = 256 * 1024
"""Bytes of repr above which a value belongs on disk rather than in kernel memory."""


class ToolOutStore(MutableMapping[str, Any]):
    """Keyed store of raw tool results, in memory.

    TODO (harness phase): spill values over `spill_threshold` to `.pkl` under the session
    directory and hand back a lazy proxy; track access order and LRU-evict resident
    values under memory pressure; run the sweep off the turn's critical path.
    """

    def __init__(self, spill_threshold: int = DEFAULT_SPILL_THRESHOLD) -> None:
        self.spill_threshold = spill_threshold
        self._values: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"ToolOutStore({len(self._values)} entries)"
