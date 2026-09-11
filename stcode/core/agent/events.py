"""
Agent events — what a caller sees while a turn runs.

Deliberately *not* a re-wrapping of the provider's stream: `TextDelta` and
`ReasoningDelta` are re-exported from `core/providers/types.py` unchanged, since a
parallel `AgentTextDelta` would be a translation layer whose only job is staying in sync.

What the agent adds is the four events a provider has no concept of — a tool starting,
a tool finishing, a turn ending, the agent giving up. `daemon/protocol.py` maps them
one-to-one onto the wire, which is why they carry primitives: a `ToolResult` with a 4 MB
payload does not belong on a socket, so `ToolFinished` carries a preview.
"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, Field

from stcode.core.providers.types import ReasoningDelta, TextDelta, Usage

PREVIEW_CHARS = 240
"""How much of a tool result rides on `ToolFinished`. Enough for a status line; the full
result is in the session."""


class ToolStarted(BaseModel):
    """A tool call was accepted and is running."""

    type: str = "tool_started"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolFinished(BaseModel):
    """A tool call returned. `ok` is False for a tool-level failure — a normal outcome
    the model reacts to, not an error in the loop."""

    type: str = "tool_finished"
    id: str
    name: str
    ok: bool = True
    preview: str = ""


class SupervisorNudge(BaseModel):
    """The supervisor spotted a loop and said something.

    An event so a client can show it: the agent is about to change direction and the
    reason should not be invisible. The nudge reaches the *model* as a `supervisor`
    record in the session, not through this.
    """

    type: str = "supervisor"
    text: str
    symptom: str = ""


class TurnFinished(BaseModel):
    """The model replied without calling a tool — the only stop condition.

    Not an `answer` dict, not a `ready` flag: models already end a turn by talking.
    `text` is the assistant's final message.
    """

    type: str = "turn_finished"
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    tool_calls: int = 0


class AgentFailed(BaseModel):
    """The turn ended without an answer: the tool-call ceiling, a provider error, or an
    interrupt. Terminal for this turn only; the agent stays usable."""

    type: str = "agent_failed"
    message: str
    recoverable: bool = True


AgentEvent = Union[
    TextDelta, ReasoningDelta, ToolStarted, ToolFinished, SupervisorNudge, TurnFinished, AgentFailed
]

__all__ = [
    "PREVIEW_CHARS",
    "AgentEvent",
    "AgentFailed",
    "ReasoningDelta",
    "SupervisorNudge",
    "TextDelta",
    "ToolFinished",
    "ToolStarted",
    "TurnFinished",
    "Usage",
]
