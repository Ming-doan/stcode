"""
Human in the loop — `ask_user_question`.

The tool exists to be used rarely. An agent that asks about every fork is slower than
doing the work and worse company than a colleague who makes reasonable calls; an agent
that never asks builds the wrong thing confidently. The line between them is whether
the answer would actually change what gets built — the docstring below is written to
push toward that test rather than toward asking.

Mechanically it is the one tool that gives control back: `runtime.ask` blocks the turn
until the UI returns an answer. A session with no user attached (a daemon run, a test)
has no `on_ask` callback, and the tool says so plainly instead of hanging — see
`Runtime.ask`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Question, Runtime, ToolError, tool


@tool(permission=ToolPermission.INTERACTIVE, spill=False)
async def ask_user_question(
    question: str,
    options: Annotated[list[str] | None, Field(max_length=4)] = None,
    header: str = "",
    multi_select: bool = False,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Ask the user a question and wait for their answer.

    Only for decisions that are genuinely theirs: a product choice with no right
    answer, a trade-off you cannot resolve from the code, an ambiguity where two
    readings lead to materially different work. Anything you can settle by reading the
    repository, following an existing convention, or picking the obvious default, settle
    yourself and say what you assumed.

    Do not use it to ask whether to continue, whether your plan looks right, or to
    report progress. Those stop the work without advancing the decision.

    Ask everything you need in one call rather than trickling questions across turns.

    Args:
        question: The full question, ending in a question mark. Be specific about what
            differs between the choices.
        options: Two to four concrete choices. Put your recommendation first and mark it
            "(recommended)". The user can always answer something else.
        header: A 1-3 word label for the decision, e.g. "Auth method", "Storage".
        multi_select: Allow more than one option to be chosen. For choices that are not
            mutually exclusive.
    """
    if not question.strip():
        raise ToolError("`question` is empty.")
    if options is not None and len(options) < 2:
        raise ToolError(
            "Give at least two options, or none at all — a single option is not a choice."
        )

    answer = await runtime.ask(
        Question(
            question=question.strip(),
            header=header.strip(),
            options=list(options or []),
            multi_select=multi_select,
        )
    )
    return f"The user answered: {answer}"


__all__ = ["ask_user_question"]
