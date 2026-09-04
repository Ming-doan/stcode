"""
Built-in tools, and the named sets an agent is spawned with.

The sets matter as much as the tools.

`MAIN_TOOLS` is what a top-level agent gets. `WORKER_TOOLS` is what `task` hands a
sub-agent, and it is deliberately smaller: no `task` and no `repl`, because a sub-agent
that can spawn is a sub-agent for which `max_depth = 1` stops bounding anything
(CLAUDE.md §4 rule 3).

`READ_ONLY_TOOLS` is defined by permission rather than by taste, so a tool that later
gains the ability to write cannot quietly stay on the list.

Two tools are registered but in no set. `repl` has no backend until step 7 and
`web_search` needs a second API key that MCP can cover instead (CLAUDE.md §6). Both are
in `BUILTIN_TOOLS` so a caller can opt in explicitly and so turning them on later is a
one-line change; neither is advertised, because being offered a capability and then
refused it costs the model a whole turn.
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

WORKER_TOOLS: tuple[str, ...] = (
    "read", "write", "edit", "bash", "bash_output", "glob", "grep", "ls",
    "todo_write", "ask_user_question", "skill",
)
"""What a sub-agent gets. See the module docstring for what is missing and why."""

MAIN_TOOLS: tuple[str, ...] = WORKER_TOOLS
"""What a top-level agent gets. Identical to `WORKER_TOOLS` for now: `task` is added by
`core/agent/` at construction (it cannot live here — a tool that spawns an `Agent` would
point `core/harness` at its own caller), and `repl` joins at step 7."""

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
