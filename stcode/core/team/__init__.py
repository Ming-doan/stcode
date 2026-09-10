"""
`core/team/` — one agent per container, coordinating through a shared volume.

Two files, and the split is the dependency arrow:

* `mailbox.py` — a message is a JSON file in `/team/inbox/<role>/`. Imports nothing
  from the harness, so the harness could import it if it ever needed to.
* `tools.py` — `send_message`, built as a factory closing over one role's mailbox.
  Registered by `Agent.enable_team()`, the same shape `task` uses.

Roles themselves are not here. They are markdown in `harness/prompts/roles/`, because
a new role must be a new file rather than a code change (CLAUDE.md 9.3).
"""

from stcode.core.team.mailbox import (
    ARTIFACTS,
    DEFAULT_TEAM_DIR,
    INBOX,
    KNOWLEDGE,
    Mailbox,
    TeamMessage,
)
from stcode.core.team.tools import make_send_message_tool

__all__ = [
    "ARTIFACTS",
    "DEFAULT_TEAM_DIR",
    "INBOX",
    "KNOWLEDGE",
    "Mailbox",
    "TeamMessage",
    "make_send_message_tool",
]
