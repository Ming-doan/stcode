"""
`task` — a sub-agent as a tool. Solo mode only.

Here rather than in `core/harness/tools/` because of the dependency arrow: this spawns
an `Agent`, and a tool inside the harness that did so would point it at its own caller.

**The docstring does two jobs.** No model is trained on this scaffold, so expect `task`
to be under-used; and a vaguely-described sub-agent is the top cause of duplicated,
off-target work. So it argues for delegation *and* forces an output format and a scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, Tool, ToolError, tool
from stcode.core.providers.gateway import Difficulty

if TYPE_CHECKING:  # pragma: no cover
    from stcode.core.agent.agent import Agent

MAX_DEPTH = 1
"""Rule 3. A sub-agent gets neither `task` nor `repl`, so this is belt and
braces — but recursion plus no budget is a fork bomb, and the cheap check is worth it."""


def make_task_tool(parent: "Agent") -> Tool[Any]:
    """Build the `task` tool bound to `parent`, the agent that spawns from it."""

    @tool(permission=ToolPermission.EXECUTE, timeout=None)
    async def task(
        prompt: str,
        name: str,
        tools: list[str] | None = None,
        scope: str | None = None,
        difficulty: Annotated[Difficulty, Field()] = "medium",
        runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
    ) -> str:
        """Hand a bounded piece of work to a sub-agent with its own context.

        Use this whenever answering something would cost you many file reads. The
        sub-agent reads the twenty files and you get back the paragraph — its tool
        output never enters your context, only its final message does. If you are about
        to read more than two or three files to answer one question, delegate instead.

        Not for small things. Spawning costs a full agent's startup, so a question a
        single `grep` answers is cheaper to answer yourself.

        Write the prompt as a briefing, not a title. It gets no follow-up question and
        cannot see your conversation, so it must contain:

        - what to do, concretely;
        - **the output format you want back** — "a table of file:line and the handler
          name", "three sentences on whether X is safe" — because a sub-agent told only
          the topic returns a transcript;
        - what not to touch.

        Run several at once by calling this multiple times in the same turn; they
        execute in parallel. Give any two that write files **disjoint** `scope`s — two
        agents that can write the same file is a design error, not a race to manage.

        Args:
            prompt: The full briefing, including the output format you expect back.
            name: Short identifier for this sub-agent, e.g. "api-scout". Appears in the
                trajectory and in its own session file.
            tools: Tool names it may use. Omit for the standard set; narrow it to
                ["read", "grep", "glob"] for anything that should only investigate.
            scope: Directory it may write inside, e.g. "src/api". Omit for read-only
                work. Two writers must never be given overlapping scopes.
            difficulty: Model tier. Use "low" for mechanical search and summarising,
                "high" only for work that needs real reasoning.
        """
        from stcode.core.agent.agent import Agent

        if runtime.depth >= MAX_DEPTH:
            raise ToolError(
                "Sub-agents cannot spawn sub-agents. Do this one yourself, or report "
                "back to your parent that it needs splitting."
            )

        child_harness = parent.harness.for_subagent(name, tools=tools, scope=scope)
        child_session = parent.session.child(name)
        child_harness.session_id = child_session.id

        child = Agent(
            gateway=parent.gateway,  # shared: closing it would kill the parent's stream
            harness=child_harness,
            session=child_session,
            difficulty=difficulty,
            max_turns=parent.max_turns,
        )
        async with child:
            await runtime.progress(f"{name}: {prompt.splitlines()[0][:80]}")
            # Every event the child produces goes out to whoever is watching, tagged
            # with this sub-agent's name. Out the side rather than into the parent's
            # stream: the parent's events are its turn, each with a record behind it,
            # and the child's belong to the child's transcript.
            return await child.result(prompt, on_event=lambda event: runtime.emit(name, event))

    return task


__all__ = ["MAX_DEPTH", "make_task_tool"]
