"""
`send_message` — the one tool team mode adds.

Everything else already exists: `/team/knowledge/` is a directory, so the ordinary file
tools work on it. What was missing is a way to tell another agent something is ready.

A factory rather than a module-level `@tool`, for the same reason as `task`: it closes
over one role's `Mailbox`, keeping `core/harness` from importing `core/team`.

**The docstring pushes `refs` over `body`, deliberately** — it is the only place that
rule can be taught, and a team's cost grows with N² if messages carry documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, Tool, ToolError, tool
from stcode.core.team.mailbox import Mailbox

MAX_BODY = 2000
"""Longer than this is a document, not a message. Refused rather than truncated —
truncating delivers something that reads complete and is not."""


def make_send_message_tool(mailbox: Mailbox) -> Tool[Any]:
    """The `send_message` tool for one role."""

    @tool(permission=ToolPermission.WRITE)
    async def send_message(
        to: str,
        subject: str,
        body: str = "",
        refs: list[str] | None = None,
        runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
    ) -> str:
        """Send a message to another agent on this team.

        Keep `body` short. Anything long belongs in a file under `/team/knowledge/` or
        `/team/artifacts/` — write it there first, then put the path in `refs`. Do not
        paste file contents into a message: the recipient can read the path, and every
        word you paste is paid for again on every turn it stays in their history.

        Send when you have finished something someone is waiting for, when you need a
        decision only another role can make, or when you have found something that
        changes their work. Your role prompt says who to report to.

        Args:
            to: The recipient's role, e.g. "ba", "frontend-dev", "backend-dev",
                "devops". It must be a role that exists on this team.
            subject: One line, so the recipient can decide whether to read it now.
            body: A short note. Say what you need and by when. Leave it empty if the
                refs speak for themselves.
            refs: Paths under /team holding the detail. This is the payload; the body
                is the covering note.
        """
        recipient = to.strip()
        if not recipient:
            raise ToolError("`to` must name a role. Use `ls /team/inbox/` to see who is on the team.")

        known = mailbox.roles()
        if known and recipient not in known:
            raise ToolError(
                f"There is no {recipient!r} on this team. The roles with an inbox are: "
                f"{', '.join(known)}."
            )
        if recipient == mailbox.role:
            raise ToolError("That is your own inbox. Use a note in /team/knowledge/ instead.")
        if len(body) > MAX_BODY:
            raise ToolError(
                f"The body is {len(body)} characters, over the {MAX_BODY} limit. Write it "
                "to /team/knowledge/ or /team/artifacts/ and send the path in `refs`."
            )

        missing = [ref for ref in (refs or []) if not _ref_exists(mailbox, ref)]
        if missing:
            raise ToolError(
                f"These refs do not exist yet: {', '.join(missing)}. Write the file first — "
                "a message pointing at nothing wastes the recipient's whole turn."
            )

        path = mailbox.send(recipient, subject.strip(), body.strip(), list(refs or []))
        await runtime.progress(f"→ {recipient}: {subject}")
        return f"Sent to {recipient}. It will be waiting at the start of their next turn ({path.name})."

    return send_message


def _ref_exists(mailbox: Mailbox, ref: Any) -> bool:
    """Whether a ref points at something real. Both spellings are accepted — absolute
    (`/team/knowledge/spec.md`) and relative to the volume (`knowledge/spec.md`)."""
    text = str(ref)
    try:
        return Path(text).exists() or (mailbox.root / text.lstrip("/")).exists()
    except OSError:
        return False


__all__ = ["MAX_BODY", "make_send_message_tool"]
