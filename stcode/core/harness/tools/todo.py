"""
Todo — `todo_write`, the tool that is not really a tool.

CLAUDE.md §5 calls it "a device to keep the plan in context. Keep it", and that framing
is the entire specification. Nothing outside the agent reads these items; writing one
changes no file and runs no command. What it does is force the plan through the model's
own output, where it stays visible for the rest of the session — a step written down is
a step that gets finished, and a multi-step task tracked only in a model's head loses
its last two items somewhere around the fourth tool call.

The one rule that matters is *exactly one* item in progress at a time. A list where four
things are `in_progress` is a list that has stopped describing what is happening.
"""

from __future__ import annotations

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext, TodoItem
from stcode.core.harness.tools.base import Runtime, ToolError, tool


@tool(permission=ToolPermission.READ, spill=False)
async def todo_write(
    todos: list[TodoItem],
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Record or update the task list for the current piece of work.

    Use it for anything that takes more than a couple of steps, and update it *as you
    go* — mark an item completed the moment it is done, not in a batch at the end. The
    list is how you and the user both keep track of where the work has got to.

    Skip it entirely for single-step tasks. A one-item todo list is ceremony.

    Each item needs both phrasings: `content` in the imperative ("Add the retry test")
    and `active_form` in the present participle ("Adding the retry test"), because the
    UI shows the second one while the item is running.

    Exactly one item may be `in_progress`. Mark an item completed only when it really
    is — if tests fail or the implementation is partial, leave it in progress and add
    an item for what is blocking it.

    Args:
        todos: The complete list, replacing whatever was there before. Send every item
            each time, not just the ones that changed.
    """
    context = runtime.context
    in_progress = [item for item in todos if item.status == "in_progress"]
    if len(in_progress) > 1:
        names = ", ".join(repr(item.content) for item in in_progress)
        raise ToolError(
            f"{len(in_progress)} items are in_progress ({names}). Exactly one at a time — "
            "pick the one you are actually working on and leave the rest pending."
        )
    for item in todos:
        if not item.active_form:
            item.active_form = item.content

    context.todos = list(todos)
    done = sum(1 for item in todos if item.status == "completed")
    return f"{context.render_todos()}\n\n[{done}/{len(todos)} complete]"


__all__ = ["todo_write"]
