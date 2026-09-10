"""
Truncation — the 8192-char forcing function, in one place.

CLAUDE.md §4 rule 1: tool output is elided, never LLM-summarised. Summarising loses
information; eliding does not — **provided the full value is somewhere the model can
actually reach**. That proviso is the whole rule, and it is what the previous version
of this module got wrong: it advertised `tool_out["read_abc"]`, a dict that lived in
the harness process while the model's REPL held a different one, so a model that did
as it was told got a `KeyError` (EXPECTED.md §4.1).

So `hint` is now a **complete clause the caller is answerable for**, not a variable
name this module wraps in backticks and hopes exists. Until `core/repl/` lands (step 7)
and `tool_out` is genuinely shared, callers pass `NARROW_REQUEST_HINT` — an instruction
that works today, because re-calling with a tighter `offset`/`limit` needs nothing but
the tool that was just called.
"""

from __future__ import annotations

DEFAULT_VIEW_LIMIT = 8192
"""Chars of tool output the agent sees per call."""

HEAD_SHARE = 0.75
"""Fraction of the budget spent on the head. The tail is where a traceback's final line,
a test summary, and a `| tail -40` all live, so it keeps a real share."""

NARROW_REQUEST_HINT = "call again with a narrower offset/limit to see the rest"
"""The recovery for a session with no REPL attached: re-call the same tool with a
tighter range. Needs nothing that may not exist."""


def tool_out_hint(output_id: str) -> str:
    """The better recovery, and only legal when the REPL really holds the value.

    Step 7 made this true: `Harness.invoke` injects the spilled payload into the
    worker's `tool_out` before the model sees the elision. Passing this hint for a
    session with no REPL would be the exact bug of EXPECTED.md 4.1 all over again, so
    `Runtime.outputs_reachable` is what decides, not this module.
    """
    return f'the whole value is in tool_out["{output_id}"] — slice it with `repl`'


_MIN_LIMIT = 32


def elide(text: str, limit: int, *, hint: str | None = None) -> str:
    """Keep the head and tail of `text`, drop the middle.

    Cutting the middle rather than the tail is deliberate: the beginning of an output
    says what ran and the end says how it went, while the bulk in between is the part
    worth fetching deliberately rather than by accident.

    Args:
        text: The full output.
        limit: Char budget for the result, marker included.
        hint: A complete clause telling the reader how to reach what was cut, e.g.
            `NARROW_REQUEST_HINT`. Only pass one you know is true — a hint naming a
            place the model cannot reach is worse than no hint, because it turns a
            visible gap into a failed retry.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    limit = max(limit, _MIN_LIMIT)
    where = f" — {hint}" if hint else ""

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


__all__ = ["DEFAULT_VIEW_LIMIT", "HEAD_SHARE", "NARROW_REQUEST_HINT", "elide", "tool_out_hint"]
