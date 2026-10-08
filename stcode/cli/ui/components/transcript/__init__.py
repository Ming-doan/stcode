"""Transcript components and formatting helpers."""

from .entries import (
    AgentRow,
    Message,
    Notice,
    PlatformNote,
    Progress,
    ShellOutput,
    Thinking,
    ToolCall,
)
from .view import Transcript
from .formatting import (
    AGENT_COLOURS,
    ARGUMENT_CHARS,
    PROGRESS_LINES,
    SHELL_OUTPUT_LINES,
    THINKING_LINES,
    TOOL_RESULT_LINES,
    agent_colour,
    format_arguments,
    platform_rule,
    shell_render,
    thinking_tail,
    tool_render,
)

__all__ = [
    "AGENT_COLOURS",
    "ARGUMENT_CHARS",
    "PROGRESS_LINES",
    "SHELL_OUTPUT_LINES",
    "THINKING_LINES",
    "TOOL_RESULT_LINES",
    "AgentRow",
    "Message",
    "Notice",
    "PlatformNote",
    "Progress",
    "ShellOutput",
    "Thinking",
    "ToolCall",
    "Transcript",
    "agent_colour",
    "format_arguments",
    "platform_rule",
    "shell_render",
    "thinking_tail",
    "tool_render",
]
