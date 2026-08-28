"""
Tool vocabulary shared by the layers that produce tools and the layers that ship them.

`ToolDefinition` lived in `core/providers/types.py` while providers were its only
consumer. It now has three: `core/harness/` builds them from Python signatures,
`core/agent/` selects which ones a turn advertises, and `core/providers/` translates
them onto each SDK's wire format. A type owned by one of its consumers invites the
dependency arrow to point the wrong way, so it moved down here — `core/common/` is a
leaf that imports from nothing else in `core/`.

`ToolResult` is the counterpart travelling the other direction. It is deliberately *not*
`providers.types.ToolResultBlock`: a block is the wire shape (an id, a string, an error
flag), while a result is what the tool actually produced — a rendered view for the
model, the untruncated payload for `tool_out`, and the metadata the trajectory log and
the TUI read. The agent loop narrows one into the other; nothing else should.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """A tool as the model sees it: a name, the prompt that teaches it, and a JSON
    Schema for the arguments.

    `input_schema` is a JSON Schema object (`{"type": "object", "properties": {...},
    "required": [...]}`). Keep it byte-stable across turns — CLAUDE.md §8 lists prompt
    caching as the single biggest cost lever, and tool definitions sit in the cached
    prefix alongside the system prompt.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolResult(BaseModel):
    """What one tool invocation produced.

    `content` is the only field that reaches the model, and it is already rendered and
    already capped. `payload` is the same information untruncated, destined for
    `tool_out[output_id]` so the agent can slice it later — that is the half of
    CLAUDE.md §2.2 that makes truncation non-lossy, and dropping it turns an elision
    into a deletion.
    """

    content: str
    is_error: bool = False
    payload: Any = None
    output_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **metadata: Any) -> "ToolResult":
        return cls(content=message, is_error=True, metadata=metadata)
