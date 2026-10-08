"""
`core/team/` — one agent per container, coordinating through a shared volume.

Two files, and the split is the dependency arrow:

* `mailbox.py` — a member is a card in `/team/members/`, a message is a JSON file in
  `/team/inbox/<role>/`. Imports nothing from the harness.
* `tools.py` — `find_teammate` and `send_team_message`, factories closing over one
  role's mailbox. Registered by `Agent.enable_team()`, the same shape `task` uses.

Roles are not here: a role is the agent's own `config.toml`, because a new role must be
a new file rather than a code change.
"""

from stcode.core.team.mailbox import (
    ARTIFACTS,
    DEFAULT_TEAM_DIR,
    INBOX,
    KNOWLEDGE,
    MEMBERS,
    Mailbox,
    TeamMember,
    TeamMessage,
)
from stcode.core.team.tools import make_find_teammate_tool, make_send_team_message_tool

__all__ = [
    "ARTIFACTS",
    "DEFAULT_TEAM_DIR",
    "INBOX",
    "KNOWLEDGE",
    "MEMBERS",
    "Mailbox",
    "TeamMember",
    "TeamMessage",
    "make_find_teammate_tool",
    "make_send_team_message_tool",
]
