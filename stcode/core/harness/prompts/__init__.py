"""
Prompts — assembling the system prompt for a turn.

One axis, not two. **Mode** — `plan` researches and cannot write; `execute` makes
changes — tracks the approval mode the user selected, so the prompt and the permission
gate never disagree about what the agent is allowed to do. A prompt that says "make the
change" while `harness/approvals.py` forbids writes produces an agent that spends its
turn discovering it is not allowed to work.

The old second axis (`orchestrator` vs `worker`) went with the RLM design in
EXPECTED.md §16. What replaced it is narrower and derived rather than declared:
`subagent=True` appends the briefing a `task` child needs, and `Harness` passes
`self.depth > 0` for it, so a spawned agent cannot be handed the wrong one.

`build_system_prompt` concatenates fixed sections, then the session's variable state.
That order is not cosmetic — see `sections.py` on prompt caching.

CLAUDE.md §4 rule 6: the agent never edits its own harness. `extra` is a harness-layer
addition the *caller* supplies; nothing here is writable from inside a turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Sequence

from stcode.core.harness.approvals import ApprovalMode
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

ROLES_DIR = Path(__file__).parent / "roles"
"""One markdown file per role. A new role is a new file, never a code change
(CLAUDE.md 9.3) — which is only true for as long as nothing here reads a role by name.
"""


def available_roles() -> list[str]:
    """Every role this build knows about. Any file in `roles/` is a valid one."""
    if not ROLES_DIR.is_dir():
        return []
    return sorted(path.stem for path in ROLES_DIR.glob("*.md"))


def load_role(name: str) -> str:
    """A role's markdown, or "" when it has none.

    An unknown name raises: a container started with `[team] role = "backedn-dev"`
    should refuse loudly, not run a nameless agent that quietly owns nothing.
    """
    if not name:
        return ""
    path = ROLES_DIR / f"{name}.md"
    if not path.is_file():
        known = ", ".join(available_roles()) or "(none installed)"
        raise FileNotFoundError(f"No role named {name!r}. Available: {known}.")
    return path.read_text(encoding="utf-8").strip()

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
    subagent: bool = False,
    cwd: str = ".",
    approval_mode: ApprovalMode = "suggest",
    tool_names: Sequence[str] = (),
    skill_catalogue: str = "",
    role: str = "",
    teammates: Sequence[str] = (),
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
        role: The role's markdown body, from `load_role`. Team mode only.
        teammates: Other roles with an inbox on the shared volume.
        mcp_catalogue: Output of `MCPManager.catalogue()` — server and tool names only.
        mcp_directory: Where the generated stubs live.
        project_instructions: Contents of the repository's CLAUDE.md / AGENTS.md.
        write_scope: Paths a sub-agent may write to, when it is narrowed.
        todos: Rendered todo list, if any.
        git: Repository state, from `git_context()`. Sampled once per session.
        extra: Harness-layer additions. Appended, never interleaved.
    """
    sections: list[str] = [IDENTITY]

    # Static first, and in a fixed order, so the cached prefix stays byte-identical
    # across turns for as long as the mode holds.
    sections.append(PLAN_MODE if mode == "plan" else EXECUTE_MODE)
    if subagent:
        sections.append(SUBAGENT)
    sections.append(TOOL_POLICY)
    if mode == "execute":
        sections.append(VERIFICATION)
    sections += [SCOPE, RECOVERY, TONE]
    # Above the optional sections and below the fixed ones: a role is static for the
    # life of a container, so it belongs in the cached prefix, and it outranks the
    # general guidance about what to work on.
    if role:
        sections.append(role_section(role, teammates))

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
    "ROLES_DIR",
    "SUBAGENT",
    "PromptMode",
    "available_roles",
    "build_system_prompt",
    "load_role",
    "mode_for",
]
