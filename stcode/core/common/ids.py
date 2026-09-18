"""
Ids — one generator, for everything that needs a name that sorts by when it was made.

A session file, a team message, anything later. It lived in `core/session/` while
sessions were the only caller, and `core/team/mailbox.py` reached across for it with a
function-level import to avoid the dependency. Two callers with nothing else in common
is the definition of `core/common/`.
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_last_id: tuple[int, int] = (0, 0)


def new_id() -> str:
    """A monotonic ULID: 48 bits of ms timestamp, 80 bits of randomness, base32.

    Sortable by creation time as a plain string — the whole reason not to use `uuid4`.
    `Session.list()` is then a directory listing needing no index and no metadata read,
    and an inbox drains oldest-first for free.

    Monotonic because "same millisecond" is not hypothetical: spawning three sub-agents
    in one turn does it, and random suffixes would order them arbitrarily. Within a
    millisecond the randomness increments, which also covers NTP stepping the clock back.
    """
    global _last_id
    stamp = int(time.time() * 1000)
    last_stamp, last_random = _last_id
    if stamp > last_stamp:
        _last_id = (stamp, secrets.randbits(80))
    else:
        _last_id = (last_stamp, last_random + 1)
    stamp, randomness = _last_id
    value = (stamp << 80) | (randomness & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


__all__ = ["new_id"]
