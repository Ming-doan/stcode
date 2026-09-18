"""
Mailbox — agent-to-agent messaging, with no protocol.

Containers share a volume, so a message is a **file**: sending writes JSON into
`/team/inbox/<role>/`, receiving is reading your own directory.

    /team/inbox/backend-dev/01HX…-from-ba.json     <- unread
    /team/inbox/backend-dev/.read/…                <- drained

No registry, no routing table, no service discovery, no N² socket mesh.

**Messages carry `refs`, not content** — that one convention is what keeps a team's
token cost from growing with the square of its size. Delivery is a write then a rename,
so a reader never sees half a message. Nothing here imports the harness.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

# ULID: sorts by time, so an inbox drains oldest-first with no index.
from stcode.core.common.ids import new_id

DEFAULT_TEAM_DIR = "/team"
"""Mounted into every container — see docs/architecture/team.md."""

INBOX = "inbox"
KNOWLEDGE = "knowledge"
ARTIFACTS = "artifacts"
READ_DIRNAME = ".read"
"""Where a drained message goes. Moved, not deleted — "who told it that?" is a question
you will ask."""


class TeamMessage(BaseModel):
    """One message. Named `TeamMessage` because `Message` is already the provider wire
    format, and two of those in one codebase is a bug waiting to happen."""

    id: str = ""
    sender: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    refs: list[str] = Field(default_factory=list)
    ts: str = ""

    def render(self) -> str:
        """As the receiving agent reads it."""
        lines = [f"[message from {self.sender or 'unknown'}] {self.subject}"]
        if self.body:
            lines.append(self.body)
        if self.refs:
            lines.append("Refs: " + ", ".join(self.refs))
        return "\n".join(lines)


class Mailbox:
    """One role's view of the shared volume: its own inbox, and everyone else's."""

    def __init__(self, root: str | Path = DEFAULT_TEAM_DIR, role: str = "") -> None:
        self.root = Path(root).expanduser()
        self.role = role

    # ---- where things are ----

    @property
    def inbox(self) -> Path:
        return self.root / INBOX / self.role

    @property
    def knowledge(self) -> Path:
        return self.root / KNOWLEDGE

    @property
    def artifacts(self) -> Path:
        return self.root / ARTIFACTS

    def inbox_of(self, role: str) -> Path:
        return self.root / INBOX / role

    def roles(self) -> list[str]:
        """Roles with an inbox on this volume — who there is to talk to."""
        directory = self.root / INBOX
        if not directory.is_dir():
            return []
        return sorted(entry.name for entry in directory.iterdir() if entry.is_dir())

    def ensure(self, *roles: str) -> None:
        """Create the volume layout. Safe to call repeatedly."""
        for path in (self.knowledge, self.artifacts, self.root / INBOX):
            path.mkdir(parents=True, exist_ok=True)
        for role in (self.role, *roles):
            if role:
                self.inbox_of(role).mkdir(parents=True, exist_ok=True)

    # ---- sending ----

    def send(
        self, to: str, subject: str, body: str = "", refs: list[str] | None = None
    ) -> Path:
        """Write one message into `to`'s inbox. Returns the file it landed in.

        Write-then-rename, so a reader draining at the same moment sees either nothing
        or a complete message.
        """
        target = self.inbox_of(to)
        target.mkdir(parents=True, exist_ok=True)

        message = TeamMessage(
            id=new_id(),
            sender=self.role,
            to=to,
            subject=subject,
            body=body,
            refs=list(refs or []),
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        path = target / f"{message.id}-from-{self.role or 'unknown'}.json"
        staging = target / f".{path.name}.partial"
        staging.write_text(message.model_dump_json(indent=2), encoding="utf-8")
        os.replace(staging, path)
        return path

    # ---- receiving ----

    def pending(self) -> list[Path]:
        """Unread message files, oldest first. What the daemon's watcher polls."""
        if not self.inbox.is_dir():
            return []
        return sorted(
            entry
            for entry in self.inbox.iterdir()
            if entry.is_file() and entry.suffix == ".json" and not entry.name.startswith(".")
        )

    def drain(self) -> list[TeamMessage]:
        """Read every waiting message and mark it read. Called at the top of a turn.

        An unparseable message is moved aside too: leaving it would mean re-draining it
        every turn forever, a louder failure than losing one malformed file.
        """
        read_dir = self.inbox / READ_DIRNAME
        messages: list[TeamMessage] = []
        for path in self.pending():
            try:
                messages.append(TeamMessage.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            read_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, read_dir / path.name)
            except OSError:
                continue
        return messages

    def history(self) -> Iterator[dict[str, Any]]:
        """Everything this role has already read. For working out what happened."""
        read_dir = self.inbox / READ_DIRNAME
        if not read_dir.is_dir():
            return
        for path in sorted(read_dir.glob("*.json")):
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

    def __repr__(self) -> str:
        return f"Mailbox({self.root}, role={self.role!r})"


__all__ = [
    "ARTIFACTS",
    "DEFAULT_TEAM_DIR",
    "INBOX",
    "KNOWLEDGE",
    "READ_DIRNAME",
    "Mailbox",
    "TeamMessage",
]
