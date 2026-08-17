"""
Provider Types — unified message, tool, and streaming-event shapes shared by every provider.

Providers translate to/from these types at their boundary, so the rest of the
agent (planning loop, tool executor, TUI) never touches a provider SDK type directly.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel

Role = Literal["user", "assistant"]

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "pause_turn",
    "refusal",
    "error",
]


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


class Message(BaseModel):
    role: Role
    content: str | list[ContentBlock]


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class TextDelta(BaseModel):
    """Incremental assistant text."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallStart(BaseModel):
    """A new tool call began; its input will arrive via ToolCallDelta events."""

    type: Literal["tool_call_start"] = "tool_call_start"
    id: str
    name: str


class ToolCallDelta(BaseModel):
    """Incremental (partial-JSON) tool call input."""

    type: Literal["tool_call_delta"] = "tool_call_delta"
    id: str
    partial_json: str


class ToolCallEnd(BaseModel):
    """A tool call finished streaming; `input` is the fully parsed argument dict."""

    type: Literal["tool_call_end"] = "tool_call_end"
    id: str
    name: str
    input: dict[str, Any]


class MessageStop(BaseModel):
    """Terminal event for a single completion — always the last event yielded."""

    type: Literal["message_stop"] = "message_stop"
    stop_reason: StopReason
    usage: Usage


StreamEvent = Union[TextDelta, ToolCallStart, ToolCallDelta, ToolCallEnd, MessageStop]
