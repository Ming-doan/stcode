"""
Built-in tools, and the named sets an agent is spawned with.

The sets matter as much as the tools. CLAUDE.md §5 says tools are only available to
sub-agents and the main agent reaches them by delegating, so `ORCHESTRATOR_TOOLS` is
`repl` and nothing else — if you find yourself adding a second entry to it, the
architecture is being undone rather than extended. Route it through a sub-agent.

`READ_ONLY_TOOLS` is the set a scout gets: it is defined by permission, not by taste,
so a tool that later gains the ability to write cannot quietly stay on the list.
"""

from stcode.core.harness.tools.base import (
    DEFAULT_MAX_OUTPUT,
    ApprovalRequest,
    Question,
    Runtime,
    Tool,
    ToolCancelled,
    ToolDenied,
    ToolError,
    ToolForbidden,
    ToolPermission,
    ToolResult,
    ToolTimeout,
    current_runtime,
    to_tool_definition,
    tool,
)
from stcode.core.harness.tools.files import edit, read, write
from stcode.core.harness.tools.interact import ask_user_question
from stcode.core.harness.tools.repl import repl
from stcode.core.harness.tools.schema import schema_from_signature
from stcode.core.harness.tools.search import glob, grep, ls
from stcode.core.harness.tools.shell import bash, bash_output
from stcode.core.harness.tools.skill import skill
from stcode.core.harness.tools.todo import todo_write
from stcode.core.harness.tools.web import web_search

BUILTIN_TOOLS: tuple[Tool[object], ...] = (
    read,
    write,
    edit,
    bash,
    bash_output,
    glob,
    grep,
    ls,
    todo_write,
    web_search,
    ask_user_question,
    skill,
    repl,
)

ORCHESTRATOR_TOOLS: tuple[str, ...] = ("repl",)
"""The RLM main agent's entire tool set (§2.1). One entry, on purpose."""

WORKER_TOOLS: tuple[str, ...] = (
    "read", "write", "edit", "bash", "bash_output", "glob", "grep", "ls",
    "todo_write", "web_search", "ask_user_question", "skill",
)
"""What a sub-agent gets by default. No `repl` — a sub-agent that can spawn a REPL can
spawn agents from it, and `max_depth` stops being the thing that bounds recursion."""

READ_ONLY_TOOLS: tuple[str, ...] = tuple(
    builtin.name for builtin in BUILTIN_TOOLS if builtin.permission is ToolPermission.READ
)
"""Derived from the declared permissions rather than hand-listed, so it cannot drift."""

__all__ = [
    "BUILTIN_TOOLS",
    "DEFAULT_MAX_OUTPUT",
    "ORCHESTRATOR_TOOLS",
    "READ_ONLY_TOOLS",
    "WORKER_TOOLS",
    "ApprovalRequest",
    "Question",
    "Runtime",
    "Tool",
    "ToolCancelled",
    "ToolDenied",
    "ToolError",
    "ToolForbidden",
    "ToolPermission",
    "ToolResult",
    "ToolTimeout",
    "ask_user_question",
    "bash",
    "bash_output",
    "current_runtime",
    "edit",
    "glob",
    "grep",
    "ls",
    "read",
    "repl",
    "schema_from_signature",
    "skill",
    "to_tool_definition",
    "todo_write",
    "tool",
    "web_search",
    "write",
]
