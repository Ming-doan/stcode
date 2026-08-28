"""
Prompts — assembling the system prompt for a turn.

Two axes, and they are independent:

* **mode** — `plan` researches and cannot write; `execute` makes changes. This tracks
  the approval mode the user selected (`/mode`), so the prompt and the permission gate
  never disagree about what the agent is allowed to do. A prompt that says "make the
  change" while `harness/approvals.py` forbids writes produces an agent that spends its
  turn discovering it is not allowed to work.
* **role** — `orchestrator` is the RLM main agent whose only tool is `repl`; `worker`
  is a sub-agent holding the real tools (CLAUDE.md §2.1).

`build_system_prompt` concatenates fixed sections, then the session's variable state.
That order is not cosmetic — see `sections.py` on prompt caching.

CLAUDE.md §2.2 rule 5: the base prompt is immutable. `/refine` may add to the harness
layer around it (`extra`, project instructions, skills) and may not rewrite what is
here.
"""

from __future__ import annotations

from typing import Literal, Sequence

from stcode.core.harness.approvals import ApprovalMode
from stcode.core.harness.prompts.execute import EXECUTE_MODE, ORCHESTRATOR, WORKER
from stcode.core.harness.prompts.plan import PLAN_MODE
from stcode.core.harness.prompts.sections import (
    IDENTITY,
    RECOVERY,
    SCOPE,
    TONE,
    TOOL_POLICY,
    VERIFICATION,
    environment_section,
    project_section,
    skills_section,
    tools_section,
)

PromptMode = Literal["plan", "execute"]
AgentRole = Literal["orchestrator", "worker"]

_APPROVAL_NOTES: dict[ApprovalMode, str] = {
    "plan": "Writes and commands are switched off. Research and propose only.",
    "suggest": "Writes and commands need the user's approval each time. Expect to be asked.",
    "auto-edit": "You may edit files freely. Running commands still needs approval.",
    "full-auto": "You may edit and run commands without asking. Be correspondingly careful.",
}


def mode_for(approval_mode: ApprovalMode) -> PromptMode:
    """The prompt mode implied by an approval mode.

    One function so the mapping exists in a single place: the moment the prompt and the
    permission gate derive this separately, they will eventually disagree.
    """
    return "plan" if approval_mode == "plan" else "execute"


def build_system_prompt(
    mode: PromptMode = "execute",
    *,
    role: AgentRole = "worker",
    cwd: str = ".",
    approval_mode: ApprovalMode = "suggest",
    tool_names: Sequence[str] = (),
    skill_catalogue: str = "",
    project_instructions: str = "",
    write_scope: str = "",
    todos: str = "",
    extra: str = "",
) -> str:
    """Assemble the system prompt.

    Args:
        mode: `plan` to research without changing anything, `execute` to do the work.
        role: `orchestrator` for the RLM main agent (whose only tool is `repl`),
            `worker` for a sub-agent with the real tools.
        cwd: The session's working directory, shown to the agent.
        approval_mode: What the agent may do unattended; also decides the note shown.
        tool_names: Tools advertised this turn.
        skill_catalogue: Output of `SkillRegistry.catalogue()`.
        project_instructions: Contents of the repository's CLAUDE.md / AGENTS.md.
        write_scope: Paths a sub-agent may write to, when it is narrowed.
        todos: Rendered todo list, if any.
        extra: Harness-layer additions (`/refine`). Appended, never interleaved — the
            base prompt above it is immutable.
    """
    sections: list[str] = [IDENTITY]

    # Static first, and in a fixed order, so the cached prefix stays byte-identical
    # across turns for as long as the mode and role hold.
    sections.append(PLAN_MODE if mode == "plan" else EXECUTE_MODE)
    if mode == "execute":
        sections.append(ORCHESTRATOR if role == "orchestrator" else WORKER)
    if role != "orchestrator":
        sections.append(TOOL_POLICY)
    if mode == "execute":
        sections.append(VERIFICATION)
    sections += [SCOPE, RECOVERY, TONE]

    for optional in (
        skills_section(skill_catalogue),
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
        )
    )
    return "\n\n".join(section for section in sections if section).strip()


__all__ = [
    "EXECUTE_MODE",
    "ORCHESTRATOR",
    "PLAN_MODE",
    "WORKER",
    "AgentRole",
    "PromptMode",
    "build_system_prompt",
    "mode_for",
]
