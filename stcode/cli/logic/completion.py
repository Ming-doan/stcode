"""Token completion independent of terminal widgets."""

from __future__ import annotations
from typing import Sequence
from stcode.cli.models import Row

TRIGGERS = ("/", "@")


def token_trigger(text: str, cursor: int) -> tuple[str, str] | None:
    """The `/` or `@` token the cursor is inside, as `(symbol, filter)`.

    A token starts at the beginning of the line or after whitespace, which is the whole
    rule: `src/api` is a path and `me@example.com` is an address, and neither should
    hijack the input. It ends at whitespace, so `/mode plan` is past the command.
    """
    if cursor <= 0 or cursor > len(text):
        return None
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:cursor]
    if not token or token[0] not in TRIGGERS:
        return None
    return token[0], token[1:]


def filter_rows(rows: Sequence[Row], query: str) -> list[Row]:
    """Narrow on a substring of the **label**, case-insensitively.

    The label only, and that is the interesting part. Searching the descriptions too
    looks generous and reads as noise: typing `/mod` would offer `/effort`, whose
    description happens to say "how hard the model should think". What you are typing
    is a name, so a name is what it matches — and `?` already lists all twelve with
    their descriptions for the times you do not know the name.

    Substring, not fuzzy. The fuzzy command palette was removed on purpose: two ways to
    reach the same twelve commands is one way for them to disagree.
    """
    if not query:
        return list(rows)
    needle = query.lower()
    return [row for row in rows if needle in row[0].lower()]
