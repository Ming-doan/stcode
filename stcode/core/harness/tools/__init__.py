"""
Built-in tools, and the named sets an agent is spawned with.

The sets matter as much as the tools:

* `MAIN_TOOLS` — a top-level agent's allowance.
* `WORKER_TOOLS` — what `task` hands a sub-agent. No `task` and no `repl`: one that can
  spawn is one for which `max_depth` bounds nothing, and one holding a persistent
  namespace is doing the parent's job without the parent's oversight.
* `READ_ONLY_TOOLS` — derived from declared permissions, so it cannot drift.

`web_search` is in both. Without `TAVILY_API_KEY` it says so on the first call rather
than being missing with no explanation.
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
from stcode.core.harness.tools.registry import ToolRegistry
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

WORKER_TOOLS: tuple[str, ...] = (
    "read", "write", "edit", "bash", "bash_output", "glob", "grep", "ls",
    "todo_write", "ask_user_question", "skill", "web_search",
)
"""What a sub-agent gets. No `task`, no `repl` — see the module docstring."""

MAIN_TOOLS: tuple[str, ...] = (*WORKER_TOOLS, "repl")
"""What a top-level agent gets. `task` is added on top by `core/agent/` at construction:
it cannot live here, because a tool that spawns an `Agent` would point `core/harness` at
its own caller."""

READ_ONLY_TOOLS: tuple[str, ...] = tuple(
    builtin.name for builtin in BUILTIN_TOOLS if builtin.permission is ToolPermission.READ
)
"""Derived from the declared permissions rather than hand-listed, so it cannot drift."""

__all__ = [
    "BUILTIN_TOOLS",
    "DEFAULT_MAX_OUTPUT",
    "MAIN_TOOLS",
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
    "ToolRegistry",
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
