"""
Approval modes — how much the agent may do without asking the human first.

This module owns the *vocabulary* and its ordering, nothing about how it's shown: the
help text and colours the chat screen renders live in `stcode/cli/labels.py`. The modes
are declared (and persisted in config) ahead of the machinery that enforces them — when
the tool layer lands it consults `ApprovalMode` rather than inventing its own names.
"""

from __future__ import annotations

from typing import Literal

ApprovalMode = Literal["plan", "suggest", "auto-edit", "full-auto"]

# Ordered least- to most-permissive. `/mode` cycles through this tuple.
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("plan", "suggest", "auto-edit", "full-auto")

DEFAULT_APPROVAL_MODE: ApprovalMode = "suggest"

def next_approval_mode(current: ApprovalMode) -> ApprovalMode:
    """The next mode in `APPROVAL_MODES`, wrapping around at the end."""
    try:
        index = APPROVAL_MODES.index(current)
    except ValueError:
        return DEFAULT_APPROVAL_MODE
    return APPROVAL_MODES[(index + 1) % len(APPROVAL_MODES)]


def parse_approval_mode(value: str) -> ApprovalMode | None:
    """Resolve a user-typed mode name (`/mode auto`) to a mode, by exact name or unique
    prefix. Returns None when it matches nothing or is ambiguous."""
    normalized = value.strip().lower().replace("_", "-")
    if normalized in APPROVAL_MODES:
        return normalized  # type: ignore[return-value]
    matches = [mode for mode in APPROVAL_MODES if mode.startswith(normalized)]
    return matches[0] if len(matches) == 1 and normalized else None
