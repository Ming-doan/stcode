"""
Session — the append-only JSONL transcript, and the only place a turn is recorded.

    s = Session.create(cwd=workspace, role="backend-dev")
    s = Session.resume("01K7...")
    Session.list(limit=20)

    s = Session.create(cwd=workspace, defer=True)   # no file until the first message

    s.append(type="user", content="Add rate limiting to the API")
    s.set_meta(model="claude-opus-5")   # a setting, appended — never a rewrite
    s.messages()        # -> list[Message] for the gateway
    s.tail(30)          # -> raw records for the supervisor
    s.meta(), s.overrides()             # what it started as / what changed since

One writer, one file, three readers. There is no second logger to keep in sync.
"""

from stcode.core.session.session import (
    DEFAULT_SESSION_DIR,
    META_BOOKKEEPING,
    MODEL_VISIBLE,
    Session,
    new_id,
    prune,
    read_records,
    sessions_dir,
)

__all__ = [
    "DEFAULT_SESSION_DIR",
    "META_BOOKKEEPING",
    "MODEL_VISIBLE",
    "Session",
    "new_id",
    "prune",
    "read_records",
    "sessions_dir",
]
