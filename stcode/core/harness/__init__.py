"""
Harness — the tools, prompts, and skills an agent works with.

    harness = await Harness.create(cwd=project_root, approval_mode="auto-edit")
    system  = harness.system_prompt(role="worker")
    tools   = harness.tool_definitions()
    result  = await harness.invoke("read", {"path": "src/main.py"})

Layout, and why each piece is where it is:

* `tools/base.py` — `@tool`, and `Runtime[T]`, the parameter the model cannot see.
* `tools/schema.py` — signature + docstring → JSON Schema. A tool is described once.
* `tools/*.py` — the built-ins, grouped by what they touch.
* `approvals.py` — the permission vocabulary and the mode policy. Tools declare a
  class; this module decides whether that class needs a human.
* `context.py` — `HarnessContext`, the workspace state tools share and the `T` above.
* `registry.py` — which tools exist and which an agent may see.
* `skills/` — `SKILL.md` discovery, loaded on demand rather than upfront.
* `prompts/` — plan and execute prompts, orchestrator and worker roles.
* `mcp.py` — external tool servers, adapted to the same `Tool` interface.
* `harness.py` — the facade that composes all of it.
"""

from stcode.core.harness.approvals import (
    APPROVAL_MODES,
    DEFAULT_APPROVAL_MODE,
    ApprovalMode,
    ToolPermission,
    is_forbidden,
    next_approval_mode,
    parse_approval_mode,
    requires_approval,
)
from stcode.core.harness.context import HarnessContext, TodoItem
from stcode.core.harness.harness import Harness, read_project_instructions
from stcode.core.harness.mcp import MCPManager, MCPServerConfig, load_mcp_config
from stcode.core.harness.prompts import AgentRole, PromptMode, build_system_prompt, mode_for
from stcode.core.harness.registry import ToolRegistry
from stcode.core.harness.skills import Skill, SkillRegistry
from stcode.core.harness.tools import (
    BUILTIN_TOOLS,
    ORCHESTRATOR_TOOLS,
    READ_ONLY_TOOLS,
    WORKER_TOOLS,
    ApprovalRequest,
    Question,
    Runtime,
    Tool,
    ToolError,
    ToolResult,
    to_tool_definition,
    tool,
)

__all__ = [
    "APPROVAL_MODES",
    "BUILTIN_TOOLS",
    "DEFAULT_APPROVAL_MODE",
    "ORCHESTRATOR_TOOLS",
    "READ_ONLY_TOOLS",
    "WORKER_TOOLS",
    "AgentRole",
    "ApprovalMode",
    "ApprovalRequest",
    "Harness",
    "HarnessContext",
    "MCPManager",
    "MCPServerConfig",
    "PromptMode",
    "Question",
    "Runtime",
    "Skill",
    "SkillRegistry",
    "TodoItem",
    "Tool",
    "ToolError",
    "ToolPermission",
    "ToolRegistry",
    "ToolResult",
    "build_system_prompt",
    "is_forbidden",
    "load_mcp_config",
    "mode_for",
    "next_approval_mode",
    "parse_approval_mode",
    "read_project_instructions",
    "requires_approval",
    "to_tool_definition",
    "tool",
]
