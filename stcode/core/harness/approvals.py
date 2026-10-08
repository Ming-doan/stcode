"""
Approval modes — how much the agent may do without asking.

The vocabulary is `core/configs.py`'s; this module owns what each mode permits.

`ToolPermission` and the policy below are the enforcement half: a tool declares its
class of side effect once, at definition, and this module — not the tool — decides
whether that class runs unattended under the current mode.
"""

from __future__ import annotations

from stcode.core.common.compat import StrEnum
from stcode.core.configs import APPROVAL_MODES, ApprovalMode  # noqa: F401 — re-exported

DEFAULT_APPROVAL_MODE: ApprovalMode = "suggest"


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
