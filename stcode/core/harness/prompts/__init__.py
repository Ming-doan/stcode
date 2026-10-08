"""
Prompts — assembling the system prompt for a turn.

One axis: **mode**. `plan` researches and cannot write, `execute` makes changes. It
tracks the approval mode, so the prompt and the permission gate never disagree — a
prompt saying "make the change" while `approvals.py` forbids writes produces an agent
that spends its turn discovering it may not work.

`subagent=True` appends the briefing a `task` child needs; `Harness` derives it from
`depth`, so a spawned agent cannot be handed the wrong one.

Fixed sections first, then the session's variable state — see `sections.py` on caching.
`extra` comes from the *caller*; nothing here is writable from inside a turn.
"""

from __future__ import annotations

from typing import Literal, Sequence

from stcode.core.configs import ApprovalMode
from stcode.core.harness.prompts.execute import EXECUTE_MODE, SUBAGENT
from stcode.core.harness.prompts.plan import PLAN_MODE
from stcode.core.harness.prompts.sections import (
    IDENTITY,
    RECOVERY,
    SCOPE,
    TONE,
    TOOL_POLICY,
    VERIFICATION,
    environment_section,
    mcp_section,
    project_section,
    role_section,
    skills_section,
    tools_section,
)

PromptMode = Literal["plan", "execute"]

_APPROVAL_NOTES: dict[ApprovalMode, str] = {
    "plan": "Writes and commands are switched off. Research and propose only.",
    "suggest": "Writes and commands need the user's approval each time. Expect to be asked.",
    "auto-edit": "You may edit files freely. Running commands still needs approval.",
    "full-auto": "You may edit and run commands without asking. Be correspondingly careful.",
}


def mode_for(approval_mode: ApprovalMode) -> PromptMode:
    """The prompt mode implied by an approval mode. One function, so the prompt and the
    permission gate cannot derive it separately and disagree."""
    return "plan" if approval_mode == "plan" else "execute"


def build_system_prompt(
    mode: PromptMode = "execute",
    *,
    subagent: bool = False,
    cwd: str = ".",
    approval_mode: ApprovalMode = "suggest",
    tool_names: Sequence[str] = (),
    skill_catalogue: str = "",
    role: str = "",
    mcp_catalogue: str = "",
    mcp_directory: str = ".stcode/mcp_servers",
    project_instructions: str = "",
    write_scope: str = "",
    todos: str = "",
    git: str = "",
    extra: str = "",
) -> str:
    """Assemble the system prompt.

    Args:
        mode: `plan` to research without changing anything, `execute` to do the work.
        subagent: True for an agent spawned by `task` — adds the one-shot briefing.
        cwd: The session's working directory, shown to the agent.
        approval_mode: What the agent may do unattended; also decides the note shown.
        tool_names: Tools advertised this turn.
        skill_catalogue: Output of `SkillRegistry.catalogue()`.
        role: The agent's prompt body, already resolved.
        mcp_catalogue: Output of `MCPManager.catalogue()` — server and tool names only.
        mcp_directory: Where the generated stubs live.
        project_instructions: Contents of the repository's CLAUDE.md / AGENTS.md.
        write_scope: Paths a sub-agent may write to, when it is narrowed.
        todos: Rendered todo list, if any.
        git: Repository state, from `git_context()`. Sampled once per session.
        extra: Harness-layer additions. Appended, never interleaved.
    """
    sections: list[str] = [IDENTITY]

    # Static first, in a fixed order, so the cached prefix stays byte-identical across
    # turns for as long as the mode holds.
    sections.append(PLAN_MODE if mode == "plan" else EXECUTE_MODE)
    if subagent:
        sections.append(SUBAGENT)
    sections.append(TOOL_POLICY)
    if mode == "execute":
        sections.append(VERIFICATION)
    sections += [SCOPE, RECOVERY, TONE]
    # Above the optional sections, below the fixed ones: a role is static for the life
    # of a container, so it belongs in the cached prefix, and it outranks the general
    # guidance about what to work on.
    if role:
        sections.append(role_section(role))

    for optional in (
        skills_section(skill_catalogue),
        mcp_section(mcp_catalogue, mcp_directory),
        project_section(project_instructions),
        extra.strip(),
    ):
        if optional:
            sections.append(optional)

    # Everything below here changes turn to turn, so it goes last.
    if tool_names:
        sections.append(tools_section(list(tool_names)))
    sections.append(
        environment_section(
            cwd=cwd,
            approval_mode=approval_mode,
            approval_note=_APPROVAL_NOTES.get(approval_mode, ""),
            todos=todos,
            scope=write_scope,
            git=git,
        )
    )
    return "\n\n".join(section for section in sections if section).strip()


__all__ = [
    "EXECUTE_MODE",
    "PLAN_MODE",
    "SUBAGENT",
    "PromptMode",
    "build_system_prompt",
    "mode_for",
]
