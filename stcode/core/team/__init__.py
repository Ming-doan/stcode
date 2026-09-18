"""
`core/team/` — one agent per container, coordinating through a shared volume.

Two files, and the split is the dependency arrow:

* `mailbox.py` — a message is a JSON file in `/team/inbox/<role>/`. Imports nothing
  from the harness.
* `tools.py` — `send_message`, a factory closing over one role's mailbox. Registered by
  `Agent.enable_team()`, the same shape `task` uses.

Roles are not here: they are markdown in `.stcode/agents/`, because a new role
must be a new file rather than a code change.
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
