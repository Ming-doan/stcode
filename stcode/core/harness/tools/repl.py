"""
`repl` — a persistent Python interpreter for the session.

The namespace lives in a subprocess (`core/repl/`) and survives between calls. Two jobs,
both about keeping bulk out of the context window:

* **MCP-as-code.** Tool definitions in the prompt prefix cost 10-30k tokens every turn;
  the same servers under `.stcode/mcp_servers/` cost a grep and an import.
* **`tool_out`.** What `elide` cut is injected here under the result's `output_id`, so
  eliding loses nothing — the whole value is one slice away.

Not for sub-agents, and not a replacement for `read`/`edit`/`bash`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from stcode.core.common.truncate import DEFAULT_VIEW_LIMIT
from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool

DEFAULT_TIMEOUT = 120.0

REPL_MAX_OUTPUT = DEFAULT_VIEW_LIMIT
"""The same cap every other tool gets. The REPL has no claim to a bigger one — the whole
point of a persistent namespace is that the bulk stays in a variable."""


@tool(permission=ToolPermission.EXECUTE, max_output=REPL_MAX_OUTPUT, spill=False)
async def repl(
    code: str,
    timeout: Annotated[float, Field(gt=0, le=900)] = DEFAULT_TIMEOUT,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Run Python in this session's persistent interpreter and return what it printed.

    The namespace persists for the whole session: a variable you set now is still there
    twenty turns from now. Use that. Assign results to names, and print only the part
    you need to reason about — output is capped at 8192 characters, and the variable
    holding the rest is still there to slice.

    Top-level `await` works — use it. Do not call `asyncio.run(...)`: it opens its own
    event loop and closes it when the cell ends, which drops anything the namespace was
    holding open, such as an MCP server connection.

    `tool_out` is a dict already in the namespace. When a tool result was elided, the
    whole value is in there under the id the elision named — slice it here rather than
    calling the tool again.

    Reach for this when a result is too big to want in full, or when you need to compute
    over something rather than read it: parse a large JSON payload and print three
    fields, filter ten thousand rows down to the four that matter, call an MCP tool from
    `.stcode/mcp_servers/`. For editing files and running commands, use the dedicated
    tools — they are shorter and their output is shaped for you.

    Args:
        code: Python to execute. Multi-line is normal; this is a cell, not a line.
        timeout: Seconds before the cell is interrupted. The namespace survives a
            timeout, so whatever the cell managed to build is still inspectable.
    """
    backend = runtime.context.repl
    if backend is None:
        raise ToolError(
            "This session has no REPL attached, so `repl` cannot run. Use the direct "
            "tools (read/write/edit/bash/glob/grep) instead."
        )

    result = await backend.execute(code, timeout=timeout, on_stream=_stream_to(runtime))
    view = result.view(REPL_MAX_OUTPUT)

    if result.ok:
        return view or "(no output)"
    # Outcome names the failure mode; the view carries the traceback. Both matter: a
    # timeout and an exception need different next moves, and `view()` alone does not
    # distinguish them.
    return f"[{result.outcome}]\n{view}" if view else f"[{result.outcome}] (no output)"


def _stream_to(runtime: Runtime[HarnessContext]):  # type: ignore[no-untyped-def]
    """Forward the cell's output to the UI as it arrives.

    A session that looks hung is a real UX problem, and a REPL cell is the longest
    thing a turn can contain. Progress here is the difference between "working" and
    "frozen".
    """

    async def on_stream(name: str, text: str) -> None:
        await runtime.progress(text)

    return on_stream


__all__ = ["DEFAULT_TIMEOUT", "REPL_MAX_OUTPUT", "repl"]
