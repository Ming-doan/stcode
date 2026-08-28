"""
Truncation — the 8192-char forcing function, in one place.

CLAUDE.md §2.2: REPL stdout is capped and never LLM-summarized. Summarizing loses
information; eliding does not, *because the full value is still reachable from the
kernel* — as `tool_out["..."]` for tool results, and as `_stdout`/`_stderr` for prints
(see `bootstrap.py`, which mirrors both streams). The elision marker names that variable
so the agent knows where to look rather than re-running the cell.
"""

from __future__ import annotations

DEFAULT_VIEW_LIMIT = 8192
"""Chars of REPL output the main agent sees per turn."""

HEAD_SHARE = 0.75
"""Fraction of the budget spent on the head. The tail is where a traceback's final line,
a test summary, and a `| tail -40` all live, so it keeps a real share."""

_MIN_LIMIT = 32


def elide(text: str, limit: int, *, hint: str | None = None) -> str:
    """Keep the head and tail of `text`, drop the middle.

    Cutting the middle rather than the tail is deliberate: the beginning of an output
    says what ran and the end says how it went, while the bulk in between is the part
    the agent should be slicing out of a variable anyway. `hint` names that variable in
    the marker.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    limit = max(limit, _MIN_LIMIT)
    where = f" — full text in `{hint}`" if hint else ""

    # The marker's own length eats into the budget, and its length depends on the
    # count it prints. Two passes is enough for the digit count to settle.
    marker = ""
    body = limit
    for _ in range(2):
        marker = f"\n… [{len(text) - body:,} chars elided{where}] …\n"
        body = limit - len(marker)
    if body <= 0:
        return text[:limit]

    head = int(body * HEAD_SHARE)
    tail = body - head
    return text[:head] + marker + (text[-tail:] if tail else "")
