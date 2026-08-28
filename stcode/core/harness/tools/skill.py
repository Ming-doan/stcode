"""
`skill` — pull a set of instructions into context on demand.

The catalogue of available skills is in the system prompt: one line each, name and
description. That is all a session pays for a skill it never uses. This tool is the
second half — the agent reads a line, recognises that the task is the one that skill
describes, and loads the instructions.

Why a tool rather than injecting every skill upfront: nine installed skills are roughly
90,000 tokens of instructions and 400 tokens of catalogue. Loading all of them would
spend most of a context window on advice about tasks the session is not doing, and
CLAUDE.md's whole premise is that context is the scarce resource.
"""

from __future__ import annotations

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool


@tool(permission=ToolPermission.READ, max_output=65536, spill=False)
async def skill(
    name: str,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Load a skill's full instructions by name.

    Skills are pre-written procedures for specific kinds of work. The catalogue in your
    system prompt lists what is available and when each applies — when a task matches
    one of those descriptions, load it *before* starting, not after getting stuck.

    What comes back is instructions for you to follow, not a report to relay. A skill
    may point at further files in its own directory; read those with `read` as it tells
    you to, rather than loading them speculatively.

    Args:
        name: The skill's name, exactly as listed in the catalogue.
    """
    registry = runtime.context.skills
    if registry is None or not len(registry):
        raise ToolError(
            "No skills are available in this session. Proceed without one — skills are "
            "an accelerator, not a prerequisite."
        )

    wanted = name.strip().lstrip("/")
    found = registry.get(wanted)
    if found is None:
        near = [candidate for candidate in registry.names() if wanted.lower() in candidate.lower()]
        suggestion = f" Did you mean: {', '.join(near)}?" if near else f" Available: {', '.join(registry.names())}."
        raise ToolError(f"No skill named {wanted!r}.{suggestion}")

    try:
        body = found.load()
    except OSError as exc:
        raise ToolError(f"Could not read {found.path}: {exc.strerror}") from exc

    # The root path is stated explicitly because the instructions reference sibling
    # files relatively ("see reference.md"), and the agent's cwd is the project, not
    # the skill. Without this line every such reference resolves to the wrong place.
    return (
        f"# Skill: {found.name}\n"
        f"Files referenced below are relative to {found.root}\n\n"
        f"{body}"
    )


__all__ = ["skill"]
