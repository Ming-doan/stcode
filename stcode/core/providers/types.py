"""
Unified message, tool and streaming-event shapes, shared by every provider.

Providers translate to and from these at their boundary, so the agent loop, the tool
executor and the TUI never touch a provider SDK type directly.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel

from stcode.core.common.tools import ToolDefinition

__all__ = [
    "ContentBlock",
    "Message",
    "MessageStop",
    "ReasoningDelta",
    "ReasoningEffort",
    "Role",
    "StopReason",
    "StreamEvent",
    "TextBlock",
    "TextDelta",
    "ToolCallDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "ToolDefinition",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
]

Role = Literal["user", "assistant"]

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
"""Union of every provider's effort scale, widest first. Providers with a
narrower scale (Anthropic has no `none`/`minimal`; Gemini's `thinking_level`
tops out at `high`) clamp or remap at their boundary — see each provider's
`stream()` docstring."""

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


# `ToolDefinition` is re-exported, not defined here: three packages need it now, so it
# lives in `core/common/tools.py`. Importing it from this module still works — every
# provider adapter already does — but new code should reach for `core.common`.


class Usage(BaseModel):
    """Token counts for one completion.

    The two cache fields stay 0 for providers that cache implicitly (OpenAI) or not at
    all. They are here because "turn 2 is ~10x cheaper" is otherwise an unverifiable
    claim: `cache_read_input_tokens` on turn 2 is the evidence, and the agent writes it
    into the session where anyone can look."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class TextDelta(BaseModel):
    """Incremental assistant text."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(BaseModel):
    """Incremental reasoning/thinking text — model's internal deliberation, not the
    final answer. Never sent back to the provider as conversation history."""

    type: Literal["reasoning_delta"] = "reasoning_delta"
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


StreamEvent = Union[TextDelta, ReasoningDelta, ToolCallStart, ToolCallDelta, ToolCallEnd, MessageStop]
