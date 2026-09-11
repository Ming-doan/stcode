"""
Truncation — the 8192-char forcing function, in one place.

Tool output is elided, never LLM-summarised: summarising loses information, eliding does
not — **provided the full value is somewhere the model can reach**.

That proviso is the whole rule, so `hint` is a **complete clause the caller is
answerable for**, not a variable name this module wraps in backticks and hopes exists.
With a REPL attached, `tool_out_hint()` names where the payload really is; without one,
`NARROW_REQUEST_HINT` says to re-call with a tighter range.
"""

from __future__ import annotations

DEFAULT_VIEW_LIMIT = 8192
"""Chars of tool output the agent sees per call."""

HEAD_SHARE = 0.75
"""Fraction of the budget spent on the head. A traceback's last line, a test summary and
a `| tail -40` all live at the end, so the tail keeps a real share."""

NARROW_REQUEST_HINT = "call again with a narrower offset/limit to see the rest"
"""Recovery when no REPL is attached: re-call the same tool with a tighter range. Needs
nothing that may not exist."""


def tool_out_hint(output_id: str) -> str:
    """The better recovery, legal only when the REPL really holds the value.

    `Harness.invoke` injects the spilled payload into the worker's `tool_out` before the
    model sees the elision. `Runtime.outputs_reachable` decides whether that happened —
    not this module.
    """
    return f'the whole value is in tool_out["{output_id}"] — slice it with `repl`'


_MIN_LIMIT = 32


def elide(text: str, limit: int, *, hint: str | None = None) -> str:
    """Keep the head and tail of `text`, drop the middle.

    The beginning says what ran and the end says how it went; the bulk between is what
    you fetch deliberately rather than by accident.

    Args:
        text: The full output.
        limit: Char budget for the result, marker included.
        hint: A complete clause saying how to reach what was cut. Only pass one you know
            is true — a hint naming a place the model cannot reach turns a visible gap
            into a failed retry.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    limit = max(limit, _MIN_LIMIT)
    where = f" — {hint}" if hint else ""

    # The marker eats into the budget and its length depends on the count it prints.
    # Two passes is enough for the digit count to settle.
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


__all__ = ["DEFAULT_VIEW_LIMIT", "HEAD_SHARE", "NARROW_REQUEST_HINT", "elide", "tool_out_hint"]
