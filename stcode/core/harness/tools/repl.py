"""
`repl` — the main agent's only tool, and the whole reason this project exists.

CLAUDE.md §2.1: the main agent has no direct tools. It writes Python; the Python calls
tools, spawns sub-agents, and holds results in variables. Everything else in
`harness/tools/` is what a *sub-agent* gets. This is what the orchestrator gets.

Why a tool at all, when a REPL is not really one: the provider wire format only has
tool calls, so `repl(code)` is how a model reaches the kernel. But the semantics are
inverted from every other tool here. A normal tool returns its output into the context;
this one returns *a view of* its output, capped at 8192 chars, with the real value left
behind in the kernel as a variable. That inversion is the architecture — see
`kernel/truncate.py` for why eliding beats summarizing.

The docstring below is doing more work than most. §9 warns that no model has been
trained on this scaffold, and the observed failure is under-using `agent()` and
over-using plain REPL code; the strategy notes are there to push back on that, and they
are the first thing to revise if the agent behaves badly.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool
from stcode.core.kernel import DEFAULT_TIMEOUT, DEFAULT_VIEW_LIMIT

REPL_MAX_OUTPUT = DEFAULT_VIEW_LIMIT
"""Exactly the kernel's own view limit. `ExecResult.view()` has already budgeted the
traceback and the trailing expression against this number, so a second, tighter cap in
the tool layer would cut what the kernel deliberately kept."""


@tool(permission=ToolPermission.EXECUTE, max_output=REPL_MAX_OUTPUT, spill=False)
async def repl(
    code: str,
    timeout: Annotated[float, Field(gt=0, le=900)] = DEFAULT_TIMEOUT,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Run Python in this session's persistent REPL and return what it printed.

    The namespace persists for the whole session: a variable you set now is still there
    twenty turns from now. Use that. Assign results to names, and print only the part
    you need to reason about — output is capped at 8192 characters, and anything beyond
    that is elided, *not* lost, because the variable is still in the kernel.

    Top-level `await` works.

    Available without importing anything:

    - `tool_out` — raw tool results by id, for slicing later.
    - `session_ctx` — a dict of knowledge shared into sub-agents you spawn.
    - `answer = {"content": ..., "ready": True}` — the only way to finish. Setting
      `ready` is what commits your final response.
    - `agent(...)`, `agent_message`, `compact`, `refine` — see below.

    How to work here, in the order it usually goes:

    1. **Orient cheaply.** One or two small calls to see the shape of the problem.
       Print little — `head`, a count, a list of paths.
    2. **Delegate anything token-heavy.** `await agent(prompt, difficulty=..., name=...,
       tools=[...], scope=...)` runs a sub-agent with its own context. It reads the
       twenty files; you get back the paragraph. This is the point of the whole
       architecture and the thing most easily forgotten — if you are about to read four
       files to answer one question, spawn an agent instead.
    3. **Fan out, then gather.** `agent()` returns as soon as the child is admitted, so
       start several and `await gather(h1, h2)` once. Sequential spawns waste the only
       real advantage you have.
    4. **Give writers disjoint scopes.** Two agents must never be able to write the same
       file. Pass `scope="src/api/"` and `scope="tests/"`, never both to both.
    5. **Verify by running something.** Tests, a build, an import. A change you have not
       executed is a change you are guessing about.
    6. **Commit the answer.** Set `answer["content"]` and `answer["ready"] = True`,
       including what you did *not* do and where you left it.

    Args:
        code: Python to execute. Multi-line is normal; this is a cell, not a line.
        timeout: Seconds before the cell is interrupted. The namespace survives a
            timeout, so whatever the cell managed to build is still inspectable.
    """
    kernel = runtime.context.kernel
    if kernel is None:
        raise ToolError(
            "This session has no REPL attached, so `repl` cannot run. Use the direct "
            "tools (read/write/edit/bash/glob/grep) instead."
        )

    result = await kernel.execute(code, timeout=timeout, on_stream=_stream_to(runtime))
    view = result.view(REPL_MAX_OUTPUT)

    if result.ok:
        return view or "(no output)"
    # Outcome names the failure mode; the view carries the traceback. Both matter: a
    # timeout and an exception need different next moves, and `view()` alone does not
    # distinguish them.
    return f"[{result.outcome}]\n{view}" if view else f"[{result.outcome}] (no output)"


def _stream_to(runtime: Runtime[HarnessContext]):  # type: ignore[no-untyped-def]
    """Forward the cell's output to the UI as it arrives.

    §9 calls a session that looks hung a real UX problem, and a REPL cell that spawns
    three sub-agents is the longest thing in a turn. Progress here is the difference
    between "working" and "frozen".
    """

    async def on_stream(name: str, text: str) -> None:
        await runtime.progress(text)

    return on_stream


__all__ = ["repl"]
