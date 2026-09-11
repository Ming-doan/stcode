"""
Tool vocabulary shared by the layers that build tools and the layers that ship them.

`core/common/` imports nothing else in `core/`, which is where a type with three
consumers belongs: `core/harness/` builds `ToolDefinition`s from signatures,
`core/agent/` selects which a turn advertises, `core/providers/` puts them on the wire.

`ToolResult` travels the other way, and is deliberately *not* `ToolResultBlock`: a block
is the wire shape, a result is what the tool produced — a rendered view for the model,
the untruncated payload for `tool_out`, and metadata for the trajectory. The agent loop
narrows one into the other.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """A tool as the model sees it: a name, the prompt that teaches it, and a JSON
    Schema for the arguments.

    Keep `input_schema` byte-stable across turns — tool definitions sit in the cached
    prefix alongside the system prompt, which is the biggest cost lever there is.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolResult(BaseModel):
    """What one tool invocation produced.

    `content` is the only field reaching the model, already rendered and already capped.
    `payload` is the same information untruncated, bound for `tool_out[output_id]` so
    the agent can slice it later — dropping it turns an elision into a deletion.
    """

    content: str
    is_error: bool = False
    payload: Any = None
    output_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **metadata: Any) -> "ToolResult":
        return cls(content=message, is_error=True, metadata=metadata)
