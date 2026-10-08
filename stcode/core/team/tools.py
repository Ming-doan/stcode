"""
`find_teammate` and `send_team_message` — the two tools team mode adds.

Everything else already exists: `/team/knowledge/` is a directory, so the ordinary file
tools work on it. What was missing is a way to find who owns the next step, and to tell
them something is ready.

Factories rather than module-level `@tool`s, for the same reason as `task`: each closes
over one role's `Mailbox`, keeping `core/harness` from importing `core/team`.

**The send docstring pushes `refs` over `body`, deliberately** — it is the only place
that rule can be taught, and a team's cost grows with N² if messages carry documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, Tool, ToolError, tool
from stcode.core.team.mailbox import Mailbox, TeamMember

MAX_BODY = 2000
"""Longer than this is a document, not a message. Refused rather than truncated —
truncating delivers something that reads complete and is not."""


def make_find_teammate_tool(mailbox: Mailbox) -> Tool[Any]:
    """The `find_teammate` tool for one role."""

    @tool(permission=ToolPermission.READ)
    async def find_teammate(query: str = "") -> str:
        """Find the teammates who could take a piece of work, before you hand it off.

        Returns each matching teammate's role and what it does. Use the role as `to` in
        `send_team_message`. Call it again rather than remembering an old answer:
        teammates join while you work.

        Args:
            query: Words describing the work, e.g. "api contract" or "deploy". A
                teammate matches if any word appears in its role or description. Leave
                empty to list everyone.
        """
        others = [member for member in mailbox.members() if member.role != mailbox.role]
        if not others:
            return "Nobody else has joined the team yet. Check again later."

        words = [word.lower() for word in query.split()]
        matches = [member for member in others if _matches(member, words)]
        if not matches:
            return (
                f"No teammate matches {query!r}. Everyone on the team:\n"
                + "\n".join(member.render() for member in others)
            )
        return "\n".join(member.render() for member in matches)

    return find_teammate


def make_send_team_message_tool(mailbox: Mailbox) -> Tool[Any]:
    """The `send_team_message` tool for one role."""

    @tool(permission=ToolPermission.WRITE)
    async def send_team_message(
        to: str,
        subject: str,
        body: str = "",
        refs: list[str] | None = None,
        runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
    ) -> str:
        """Hand off work to, or ask something of, another agent on this team.

        Use `find_teammate` first to learn which role owns the work.

        Keep `body` short. Anything long belongs in a file under `/team/knowledge/` or
        `/team/artifacts/` — write it there first, then put the path in `refs`. Do not
        paste file contents into a message: the recipient can read the path, and every
        word you paste is paid for again on every turn it stays in their history.

        Send when you have finished something someone is waiting for, when you need a
        decision only another role can make, or when you have found something that
        changes their work. Your role prompt says who to report to.

        Args:
            to: The recipient's role, exactly as `find_teammate` shows it.
            subject: One line, so the recipient can decide whether to read it now.
            body: A short note. Say what you need and by when. Leave it empty if the
                refs speak for themselves.
            refs: Paths under /team holding the detail. This is the payload; the body
                is the covering note.
        """
        recipient = to.strip()
        if not recipient:
            raise ToolError("`to` must name a role. Use `find_teammate` to see who is on the team.")
        if recipient == mailbox.role:
            raise ToolError("That is your own inbox. Use a note in /team/knowledge/ instead.")

        # A card, not merely an inbox: a message to a role that never joined would sit
        # in a directory nobody drains, and the sender would believe it was delivered.
        known = [member.role for member in mailbox.members() if member.role != mailbox.role]
        if recipient not in known:
            raise ToolError(
                f"There is no {recipient!r} on this team. The roles that have joined are: "
                f"{', '.join(known) or '(none yet)'}. Use `find_teammate` to find who owns the work."
            )
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

    return send_team_message


def _matches(member: TeamMember, words: list[str]) -> bool:
    if not words:
        return True
    text = f"{member.role} {member.description}".lower()
    return any(word in text for word in words)


def _ref_exists(mailbox: Mailbox, ref: Any) -> bool:
    """Whether a ref points at something real. Both spellings are accepted — absolute
    (`/team/knowledge/spec.md`) and relative to the volume (`knowledge/spec.md`)."""
    text = str(ref)
    try:
        return Path(text).exists() or (mailbox.root / text.lstrip("/")).exists()
    except OSError:
        return False


__all__ = ["MAX_BODY", "make_find_teammate_tool", "make_send_team_message_tool"]
