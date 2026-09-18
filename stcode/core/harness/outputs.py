"""
`OutputStore` — where elided tool output is parked, with a ceiling.

Rule 1 says output is elided rather than summarised, *provided the whole value stays
somewhere the model can reach*. That place is this mapping (and the REPL's `tool_out`,
which mirrors it). Nothing ever removed an entry, so a long session paid for every
large result it had produced since it started, for as long as it ran.

A ceiling instead: newest wins, oldest goes. The trade is deliberate — the alternative
to dropping the oldest spill is a daemon that grows until it is killed, and an
`output_id` from forty tool calls ago is one nobody is going to slice.

Beside `harness.py`, not in `core/common/`: the harness is the only thing that holds
one. `elide` is shared because `core/repl/` uses it too; the store is not.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterator, MutableMapping

MAX_ENTRIES = 32
"""Spilled results kept. A turn tops out at 40 tool calls and few of them spill, so
this reaches back further than any context the model still has."""

MAX_CHARS = 8 * 1024 * 1024
"""Total size of the text held, across every entry. A single 8 MB result therefore
evicts everything else and stays — which is right: it is the one just referred to."""


def size_of(value: Any) -> int:
    """How much of the budget a value spends. Text is measured; anything else is free.

    Not `sys.getsizeof` or a pickle round-trip: the thing being bounded is transcript
    bulk, and a structure the tool parsed for us is small precisely because it is
    structured.
    """
    if isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return 0


class OutputStore(MutableMapping[str, Any]):
    """An insertion-ordered mapping that evicts the oldest entry when full."""

    def __init__(self, *, max_entries: int = MAX_ENTRIES, max_chars: int = MAX_CHARS) -> None:
        self.max_entries = max_entries
        self.max_chars = max_chars
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._chars = 0

    # ---- mapping ----

    def __getitem__(self, key: str) -> Any:
        value = self._items[key]
        # Reading counts as use: the entry the model just sliced is the one most likely
        # to be sliced again.
        self._items.move_to_end(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._items:
            self._chars -= size_of(self._items.pop(key))
        self._items[key] = value
        self._chars += size_of(value)
        self._evict()

    def __delitem__(self, key: str) -> None:
        self._chars -= size_of(self._items.pop(key))

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"OutputStore({len(self._items)} entries, {self._chars:,} chars)"

    # ---- the ceiling ----

    @property
    def chars(self) -> int:
        """Text currently held. What the ceiling is measured against."""
        return self._chars

    def _evict(self) -> None:
        """Drop oldest-first until both ceilings hold.

        Never drops the entry just written, even when it alone is over the char budget:
        the caller has already told the model where to find it.
        """
        while len(self._items) > self.max_entries or (
            self._chars > self.max_chars and len(self._items) > 1
        ):
            self._chars -= size_of(self._items.popitem(last=False)[1])


__all__ = ["MAX_CHARS", "MAX_ENTRIES", "OutputStore", "size_of"]
