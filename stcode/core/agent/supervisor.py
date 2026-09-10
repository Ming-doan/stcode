"""
Supervisor — a second pair of eyes on the trajectory.

NVIDIA AVO reached 100% on ARC-AGI-3 and credited system design rather than a stronger
model. The component they name is a supervisor watching for **stagnation and repeated
unproductive cycles**, redirecting the main agent when it finds one.

We already write the trajectory — it is the session file — so this is just a reader.

## How it stays cheap

Counting first, model second:

1. Every N tool-call iterations, look at the last 30 records.
2. Four **heuristics**, plain Python, zero tokens: the same call repeated, more than
   half the calls failing, lots of work and no file written, the same file rewritten
   over and over.
3. Only if one fires, ask a `difficulty="low"` model what to try instead.
4. That model may answer NONE. Most warnings are not stagnation, and a supervisor that
   cannot say "carry on" is a supervisor that interrupts good work.

So it is always on and usually free.

## Three design rules

**Nudges go into `messages`, never the system prompt.** Written as a `supervisor`
record, which `Session.messages()` turns into a prefixed `user` message. The cached
prefix stays byte-identical — editing the system prompt would mean a full cache miss
on every nudge, which is a strange price to pay for advice.

**The supervisor has no tools.** It reads and it speaks. One with write access is a
second agent, and then you have to answer "who supervises the supervisor?".

**It checks inside a turn, not between turns.** A task that loops does so within one
turn, iterating toward `max_turns`. Checking once per user message would miss exactly
the failure this exists to catch.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import aclosing
from typing import Any, Sequence

from stcode.core.providers.gateway import Difficulty, LLMGateway
from stcode.core.providers.types import Message, TextDelta

DEFAULT_EVERY = 8
"""Tool-call iterations between checks. Low enough to catch a loop before it burns the
turn's budget, high enough that a productive stretch is never interrupted."""

DEFAULT_WINDOW = 30
"""Records the heuristics look at. About two iterations' worth of calls and results."""

REPEAT_THRESHOLD = 3
ERROR_RATE_THRESHOLD = 0.5
SAME_FILE_THRESHOLD = 4
IDLE_CALLS_THRESHOLD = 10
"""Tool calls with no file written before "lots of looking, no doing" counts as a
symptom. High on purpose: research legitimately writes nothing, and a supervisor that
fires on reading is one you switch off."""

WRITING_TOOLS = frozenset({"write", "edit"})

NO_NUDGE = "NONE"

SYSTEM = """\
You are watching another agent work, and you can see the last few minutes of what it \
did. Your only question: is it making progress, or is it stuck in a loop?

Reply with exactly the word NONE if it is working normally. Trying something twice, \
reading before editing, and a failed command followed by a fixed one are all normal.

Otherwise reply with one or two sentences, addressed to the agent, that name what it \
keeps doing and what to do instead. Be concrete — name the file, the command, or the \
assumption you think is wrong. No preamble, no praise, no questions."""


class Supervisor:
    """Watches a session and, rarely, says something."""

    def __init__(
        self,
        gateway: LLMGateway,
        *,
        every: int = DEFAULT_EVERY,
        window: int = DEFAULT_WINDOW,
        difficulty: Difficulty = "low",
    ) -> None:
        self.gateway = gateway
        self.every = every
        self.window = window
        self.difficulty = difficulty
        self._last_nudge = ""
        """The previous nudge. Saying the same thing twice is itself a loop, and an
        agent that ignored the advice once will ignore the repeat."""

    async def check(self, records: Sequence[dict[str, Any]]) -> str | None:
        """A nudge for the agent, or None. Costs nothing unless a heuristic fires."""
        recent = list(records)[-self.window :]
        symptom = self.smell(recent)
        if symptom is None:
            return None
        nudge = await self.diagnose(recent, symptom)
        if nudge is None or nudge == self._last_nudge:
            return None
        self._last_nudge = nudge
        return nudge

    def due(self, iteration: int) -> bool:
        """Whether this iteration is a checkpoint. Never the first one — there is no
        trajectory to read yet."""
        return iteration > 0 and iteration % self.every == 0

    # ---- the free half ----

    @staticmethod
    def smell(records: Sequence[dict[str, Any]]) -> str | None:
        """Four countable signs of being stuck. No model, no tokens.

        Returns the symptom in plain words, because that sentence is what the diagnosis
        call is given to explain — a boolean would tell the cheap model nothing.
        """
        calls = [r for r in records if r.get("type") == "tool_call"]
        results = [r for r in records if r.get("type") == "tool_result"]

        repeated = _most_repeated(calls)
        if repeated and repeated[1] >= REPEAT_THRESHOLD:
            return f"it called `{repeated[0]}` with identical arguments {repeated[1]} times"

        if len(results) >= 4:
            failures = sum(1 for r in results if r.get("is_error"))
            if failures / len(results) > ERROR_RATE_THRESHOLD:
                return f"{failures} of its last {len(results)} tool calls failed"

        edited = _most_edited(calls)
        if edited and edited[1] >= SAME_FILE_THRESHOLD:
            return f"it has written to {edited[0]} {edited[1]} times"

        if len(calls) >= IDLE_CALLS_THRESHOLD and not any(
            call.get("name") in WRITING_TOOLS for call in calls
        ):
            return f"{len(calls)} tool calls in a row without writing a single file"

        return None

    # ---- the cheap half ----

    async def diagnose(self, records: Sequence[dict[str, Any]], symptom: str) -> str | None:
        """Ask a small model whether the symptom is really stagnation.

        A veto, not a rubber stamp: NONE means the heuristic was a false alarm, which
        it usually is. A failure here returns None — a broken supervisor must never be
        able to end a turn.
        """
        prompt = (
            f"What the counters noticed: {symptom}.\n\n"
            f"What the agent has been doing:\n{render_trajectory(records)}\n\n"
            "Is it stuck? Reply NONE, or one or two sentences of redirection."
        )
        try:
            reply = ""
            # `aclosing` for the same reason the agent loop uses it everywhere: an
            # abandoned provider stream stays suspended holding a connection, and may
            # be finalised after the event loop has closed.
            async with aclosing(
                self.gateway.stream(
                    [Message(role="user", content=prompt)],
                    system=SYSTEM,
                    difficulty=self.difficulty,
                )
            ) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        reply += event.text
        except Exception:  # noqa: BLE001 — advice is optional; the turn is not
            return None

        answer = reply.strip()
        if not answer or answer.upper().startswith(NO_NUDGE):
            return None
        return answer


def render_trajectory(records: Sequence[dict[str, Any]]) -> str:
    """The recent trajectory as a few short lines.

    Deliberately lossy. The supervisor is looking for a *shape* — the same call over
    and over, errors piling up — and sending it whole file contents would cost more
    than the loop it is trying to prevent.
    """
    lines: list[str] = []
    for record in records:
        kind = record.get("type")
        if kind == "tool_call":
            lines.append(f"call {record.get('name')}({_short(record.get('arguments'))})")
        elif kind == "tool_result":
            mark = "ERROR" if record.get("is_error") else "ok"
            lines.append(f"  -> {mark}: {_short(record.get('content'), 120)}")
        elif kind == "assistant" and record.get("content"):
            lines.append(f"said: {_short(record.get('content'), 160)}")
        elif kind == "user":
            lines.append(f"user: {_short(record.get('content'), 160)}")
    return "\n".join(lines)


def _short(value: Any, limit: int = 100) -> str:
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _signature(call: dict[str, Any]) -> str:
    return json.dumps(
        [call.get("name"), call.get("arguments")], sort_keys=True, default=str
    )


def _most_repeated(calls: Sequence[dict[str, Any]]) -> tuple[str, int] | None:
    """The most-repeated (tool, arguments) pair, by tool name."""
    if not calls:
        return None
    counts = Counter(_signature(call) for call in calls)
    signature, count = counts.most_common(1)[0]
    name = json.loads(signature)[0]
    return (str(name), count)


def _most_edited(calls: Sequence[dict[str, Any]]) -> tuple[str, int] | None:
    """The file written to most often in the window."""
    paths = Counter(
        str((call.get("arguments") or {}).get("path", ""))
        for call in calls
        if call.get("name") in WRITING_TOOLS
    )
    paths.pop("", None)
    if not paths:
        return None
    path, count = paths.most_common(1)[0]
    return (path, count)


__all__ = [
    "DEFAULT_EVERY",
    "DEFAULT_WINDOW",
    "Supervisor",
    "render_trajectory",
]
