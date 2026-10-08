"""
Mailbox — agent-to-agent messaging, with no protocol.

Containers share a volume, so a message is a **file**: sending writes JSON into
`/team/inbox/<role>/`, receiving is reading your own directory. Joining writes a card
to `/team/members/<role>.json`, and finding a teammate is reading those.

    /team/members/backend-dev.json                 <- who it is, what it does
    /team/inbox/backend-dev/01HX…-from-ba.json     <- unread
    /team/inbox/backend-dev/.read/…                <- drained

No registry service, no routing table, no N² socket mesh. Join, list members and send
are the three operations a team service would also have to offer — see
docs/architecture/team.md.

**Messages carry `refs`, not content** — that one convention is what keeps a team's
token cost from growing with the square of its size. Delivery is a write then a rename,
so a reader never sees half a message. Nothing here imports the harness.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

# ULID: sorts by time, so an inbox drains oldest-first with no index.
from stcode.core.common.ids import new_id

DEFAULT_TEAM_DIR = "/team"
"""Mounted into every container — see docs/architecture/team.md."""

INBOX = "inbox"
MEMBERS = "members"
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


class TeamMember(BaseModel):
    """One agent's card: who it is and what it does. What `find_teammate` reads."""

    role: str
    team: str = ""
    description: str = ""
    host: str = ""
    joined: str = ""

    def render(self) -> str:
        """As a teammate looking for someone reads it."""
        line = f"{self.role} — {self.description or '(no description)'}"
        return f"{line} (on {self.host})" if self.host else line


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

    @property
    def members_dir(self) -> Path:
        return self.root / MEMBERS

    def inbox_of(self, role: str) -> Path:
        return self.root / INBOX / role

    def ensure(self, *roles: str) -> None:
        """Create the volume layout. Safe to call repeatedly."""
        for path in (self.knowledge, self.artifacts, self.members_dir, self.root / INBOX):
            path.mkdir(parents=True, exist_ok=True)
        for role in (self.role, *roles):
            if role:
                self.inbox_of(role).mkdir(parents=True, exist_ok=True)

    # ---- membership ----

    def join(self, description: str = "", team: str = "") -> Path:
        """Create this role's inbox and write its card. Returns the card's path.

        A restart rewrites the card; stopping leaves it, because the inbox outlives the
        process and mail sent while a role is down waits for it.
        """
        self.ensure()
        member = TeamMember(
            role=self.role,
            team=team,
            description=description.strip(),
            host=socket.gethostname(),
            joined=_now(),
        )
        path = self.members_dir / f"{self.role}.json"
        _write_atomic(path, member.model_dump_json(indent=2))
        return path

    def members(self) -> list[TeamMember]:
        """Every card on the volume, by role. One unreadable card is skipped, not fatal:
        a broken file on a shared volume must not hide the whole team."""
        if not self.members_dir.is_dir():
            return []
        found: list[TeamMember] = []
        for path in sorted(self.members_dir.glob("*.json")):
            try:
                found.append(TeamMember.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return found

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
            ts=_now(),
        )
        path = target / f"{message.id}-from-{self.role or 'unknown'}.json"
        _write_atomic(path, message.model_dump_json(indent=2))
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_atomic(path: Path, text: str) -> None:
    """Write-then-rename, so a reader at the same moment sees the old file or the new
    one, never half of either. The staging name starts with a dot and ends in
    `.partial`, so no reader's `*.json` matches it."""
    staging = path.with_name(f".{path.name}.partial")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


__all__ = [
    "ARTIFACTS",
    "DEFAULT_TEAM_DIR",
    "INBOX",
    "KNOWLEDGE",
    "MEMBERS",
    "READ_DIRNAME",
    "Mailbox",
    "TeamMember",
    "TeamMessage",
]
