"""
Approval modes — how much the agent may do without asking.

Owns the *vocabulary* and its ordering, nothing about how it is shown; the help text and
colours live in `stcode/cli/labels.py`.

`ToolPermission` and the policy below are the enforcement half: a tool declares its
class of side effect once, at definition, and this module — not the tool — decides
whether that class runs unattended under the current mode.
"""

from __future__ import annotations

from stcode.core.common.compat import StrEnum
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

    Declared once at definition; the policy below decides what it means under the
    current mode. Tools never test the mode themselves — one that knows about
    `full-auto` will eventually disagree with the next one about what it means.
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
# The ordering is about *workspace mutation*: each mode adds one class to the previous —
# nothing, then writes, then arbitrary execution. NETWORK is orthogonal (the risk is
# disclosure, not corruption), so it is gated on its own rather than wedged in.
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
    # `plan` is a research mode: a mid-plan "may I write this file?" prompt would
    # defeat it rather than enforce it. Network is not refused — it only mutates what
    # the agent knows — but it asks, because egress is a disclosure to authorise.
    "plan": frozenset({ToolPermission.WRITE, ToolPermission.EXECUTE}),
}


def requires_approval(mode: ApprovalMode, permission: ToolPermission) -> bool:
    """Whether a tool of this permission class must ask the human before running."""
    return permission not in _UNATTENDED.get(mode, _UNATTENDED[DEFAULT_APPROVAL_MODE])


def is_forbidden(mode: ApprovalMode, permission: ToolPermission) -> bool:
    """Whether this mode refuses the tool outright, with no prompt to offer.

    Distinct from `requires_approval`: "ask first" is a pause a human can resolve, while
    this is a capability the mode does not have. The agent needs to know which it hit —
    retrying a forbidden tool is never going to work.
    """
    return permission in _FORBIDDEN.get(mode, frozenset())
