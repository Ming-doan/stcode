"""
Session — the append-only JSONL transcript, and the only place a turn is recorded.

    s = Session.create(cwd=workspace, role="backend-dev")
    s = Session.resume("01K7...")
    Session.list(limit=20)

    s.append(type="user", content="Add rate limiting to the API")
    s.messages()        # -> list[Message] for the gateway
    s.tail(30)          # -> raw records for the supervisor

One writer, one file, three readers. There is no second logger to keep in sync — see
CLAUDE.md §4 rule 7 for why the gateway does not get one.
"""

from stcode.core.session.session import (
    DEFAULT_SESSION_DIR,
    MODEL_VISIBLE,
    Session,
    new_id,
    prune,
    read_records,
    sessions_dir,
)

__all__ = [
    "DEFAULT_SESSION_DIR",
    "MODEL_VISIBLE",
    "Session",
    "new_id",
    "prune",
    "read_records",
    "sessions_dir",
]
