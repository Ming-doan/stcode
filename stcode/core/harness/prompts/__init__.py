"""
Prompts — assembling the system prompt for a turn.

One axis: **mode**. `plan` researches and cannot write, `execute` makes changes. It
tracks the approval mode, so the prompt and the permission gate never disagree — a
prompt saying "make the change" while `approvals.py` forbids writes produces an agent
that spends its turn discovering it may not work.

`subagent=True` appends the briefing a `task` child needs; `Harness` derives it from
`depth`, so a spawned agent cannot be handed the wrong one.

A **role** is the third input, and it is not shipped here: an agent profile is a
`config.toml` under `.stcode/agents/` or `~/.stcode/agents/`, and `load_agent_prompt`
reads its `[agent] prompt`. Only the mode prompts are code.

Fixed sections first, then the session's variable state — see `sections.py` on caching.
`extra` comes from the *caller*; nothing here is writable from inside a turn.
"""

from __future__ import annotations

import os
from stcode.core.common.compat import tomllib
from pathlib import Path
from typing import Literal, Sequence

from stcode.core.common.paths import config_dir
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

AGENTS_DIRNAME = "agents"
"""Where an agent profile lives, under the project's `.stcode/` or the user's config
directory. **One `config.toml` per agent**, named by the role."""

AGENT_PROFILE_SUFFIX = ".toml"
"""A profile is an ordinary config file. That is the whole point: an agent is a prompt
*and* the settings it runs under, and while those were a markdown file and a TOML file
every deployment had to mount two things and keep them in step. One file is one mount —
`-v ./agents/backend-dev.toml:/config/config.toml` — and there is no second format to
validate."""

AGENTS_DIR_ENV = "STCODE_AGENTS_DIR"
"""Points the search at one directory and nothing else. What a container mounts."""


def agents_dirs(cwd: str | Path | None = None) -> list[Path]:
    """Where a role is looked for, most specific first.

    Roles are **not** shipped in the package. A role is a deployment fact — this
    container is the backend dev — and a build that carried four of them made adding a
    fifth a release. `examples/agents/` in the repository is a starting point to copy,
    not a fallback: a silent fallback would mean a container whose mount failed still
    starts, as somebody else's backend dev.
    """
    override = os.environ.get(AGENTS_DIR_ENV)
    if override:
        return [Path(override).expanduser()]

    root = Path(cwd).expanduser() if cwd else Path.cwd()
    return [root / ".stcode" / AGENTS_DIRNAME, config_dir() / AGENTS_DIRNAME]


def resolve_prompt(prompt: str, prompt_file: str, source: Path | None = None) -> str:
    """An `[agent]` prompt from the two keys that can carry it.

    One function, because two callers ask the same question about the same pair of
    keys: `configs.resolve_agent_prompt` for the config in hand, and
    `load_agent_prompt` for a profile found by name. A second implementation would be a
    second set of rules about which key wins.

    `prompt_file` is relative to `source` — the file the keys were read from — so a
    directory of profiles moves as a unit. Naming both keys is a `ValueError`: a
    precedence rule here would be one more thing to remember on the day a container
    comes up with the wrong prompt.
    """
    where = f" in {source}" if source else ""
    if prompt.strip() and prompt_file.strip():
        raise ValueError(
            f"[agent] prompt and [agent] prompt_file are both set{where} — an agent "
            "profile carries one prompt, inline or in a file."
        )
    if prompt.strip():
        return prompt.strip()
    if not prompt_file.strip():
        return ""
    target = Path(prompt_file).expanduser()
    if not target.is_absolute():
        target = (source.parent if source else Path.cwd()) / target
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        # Loud, like an unknown role: a prompt file that did not mount is an agent that
        # owns nothing, and it looks exactly like one that did until it starts working.
        raise FileNotFoundError(
            f"[agent] prompt_file{where} points at {target}, which cannot be read ({exc})."
        ) from exc


def available_agents(cwd: str | Path | None = None) -> list[str]:
    """Every agent profile installed on this machine, nearest directory first."""
    names: list[str] = []
    for directory in agents_dirs(cwd):
        if not directory.is_dir():
            continue
        names += [
            path.stem
            for path in directory.glob(f"*{AGENT_PROFILE_SUFFIX}")
            if path.stem not in names
        ]
    return sorted(names)


def find_agent_profile(name: str, cwd: str | Path | None = None) -> Path:
    """Where `name`'s profile is, or raise saying what is installed.

    An unknown name raises: a container started with a typo'd role, or with the agents
    volume unmounted, should refuse loudly rather than run an agent that owns nothing.
    There is deliberately no bundled fallback — a silent one would mean that container
    starts anyway, as somebody else's backend dev.
    """
    searched = agents_dirs(cwd)
    for directory in searched:
        path = directory / f"{name}{AGENT_PROFILE_SUFFIX}"
        if path.is_file():
            return path
    known = ", ".join(available_agents(cwd)) or "(none installed)"
    raise FileNotFoundError(
        f"No agent named {name!r} in {' or '.join(str(path) for path in searched)}. "
        f"Available: {known}. Copy one from examples/agents/, or set "
        f"{AGENTS_DIR_ENV} to where yours live."
    )


def load_agent_prompt(name: str, cwd: str | Path | None = None) -> str:
    """The `[agent] prompt` of `name`'s profile, or "" when the session has no role.

    **Only the prompt.** A profile found by name contributes the thing that makes it
    that agent; it does not get a second opinion about the model, the socket or the
    session directory — those come from the config that was actually loaded. Two files
    both claiming to configure the daemon is a support question about which one won.

    Mounted *as* the config (`STCODE_CONFIG=/config/backend-dev.toml`) the whole file
    applies, and this lookup never runs: `resolve_agent_prompt` reads the prompt
    straight off the config in hand.

    `tomllib` directly rather than `core/configs`, which imports this package — the
    prompt is a string in a TOML file, and reaching back up for a validated model to
    get it would be rule 7 in miniature.
    """
    if not name:
        return ""
    path = find_agent_profile(name, cwd)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc

    agent = data.get("agent") or {}
    body = resolve_prompt(
        str(agent.get("prompt", "") or ""), str(agent.get("prompt_file", "") or ""), path
    )
    if not body:
        # A profile with no prompt is a mount that half-happened. Loud, for the same
        # reason an unknown name is.
        raise ValueError(
            f"{path} has no [agent] prompt — an agent profile without a prompt is an "
            "agent that owns nothing."
        )
    return body

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
        role: The agent's prompt body, already resolved. Team mode only.
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
    "AGENTS_DIRNAME",
    "AGENTS_DIR_ENV",
    "SUBAGENT",
    "PromptMode",
    "AGENT_PROFILE_SUFFIX",
    "agents_dirs",
    "available_agents",
    "build_system_prompt",
    "find_agent_profile",
    "load_agent_prompt",
    "resolve_prompt",
    "mode_for",
]
