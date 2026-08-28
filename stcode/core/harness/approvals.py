"""
Approval modes — how much the agent may do without asking the human first.

This module owns the *vocabulary* and its ordering, nothing about how it's shown: the
help text and colours the chat screen renders live in `stcode/cli/labels.py`.

`ToolPermission` and the policy below are the enforcement half: a tool declares what
class of side effect it has, once, at definition; this module — not the tool — decides
whether the current mode lets that class run unattended.
"""

from __future__ import annotations

from enum import StrEnum
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


class ToolPermission(StrEnum):
    """What class of side effect a tool has — the axis approval modes gate on.

    A tool declares one of these once, at definition (`@tool(permission=...)`), and the
    policy below decides whether that class needs a human in the loop under the current
    mode. Tools never test the mode themselves: a tool that knows about `full-auto` is a
    tool that will disagree with the next one about what `full-auto` means.
    """

    READ = "read"
    """Observes the workspace and nothing else. Safe in every mode, `plan` included."""

    WRITE = "write"
    """Mutates the filesystem. The thing `plan` exists to forbid."""

    EXECUTE = "execute"
    """Runs an arbitrary command or arbitrary Python. Strictly wider than WRITE — a
    shell can write files, and it can also delete the repository."""

    NETWORK = "network"
    """Leaves the machine. Gated separately from WRITE because the risk is disclosure,
    not corruption: the workspace survives, its contents may not stay private."""

    INTERACTIVE = "interactive"
    """Asks the human something. Always allowed — a mode that blocks the agent from
    asking permission-shaped questions has the gate backwards."""


# Which permissions run unattended, which ask, and which are refused outright.
#
# The mode ordering is about *workspace mutation*, and along that axis each mode adds
# one class to the previous: nothing, then writes, then arbitrary execution. NETWORK is
# orthogonal to it — the risk there is disclosure, not corruption — so it is gated on
# its own line rather than wedged into the ordering.
_UNATTENDED: dict[ApprovalMode, frozenset[ToolPermission]] = {
    "plan": frozenset({ToolPermission.READ, ToolPermission.INTERACTIVE}),
    "suggest": frozenset({ToolPermission.READ, ToolPermission.INTERACTIVE}),
    "auto-edit": frozenset(
        {
            ToolPermission.READ,
            ToolPermission.INTERACTIVE,
            ToolPermission.WRITE,
            ToolPermission.NETWORK,
        }
    ),
    "full-auto": frozenset(ToolPermission),
}

_FORBIDDEN: dict[ApprovalMode, frozenset[ToolPermission]] = {
    # `plan` is a research mode: the human reads a plan before a byte changes, so a
    # mid-plan "may I write this file?" prompt would defeat it rather than enforce it.
    # Network research is *not* refused here — it only mutates what the agent knows —
    # it just has to ask, because egress is a disclosure the user should authorise.
    "plan": frozenset({ToolPermission.WRITE, ToolPermission.EXECUTE}),
}


def requires_approval(mode: ApprovalMode, permission: ToolPermission) -> bool:
    """Whether a tool of this permission class must ask the human before running."""
    return permission not in _UNATTENDED.get(mode, _UNATTENDED[DEFAULT_APPROVAL_MODE])


def is_forbidden(mode: ApprovalMode, permission: ToolPermission) -> bool:
    """Whether this mode refuses the tool outright, with no approval prompt to offer.

    Distinct from `requires_approval` on purpose: "ask first" is a pause the human can
    resolve, while this is a capability the mode does not have. The agent should be told
    which one it hit — retrying a forbidden tool is never going to work.
    """
    return permission in _FORBIDDEN.get(mode, frozenset())
