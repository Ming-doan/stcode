"""
`full-auto` without an approver runs only inside a container.

Code rather than a document, because a document cannot refuse. `guard_autonomy` runs
before the daemon binds and again per session. **There is no override flag.**

**`has_approver` is False for every daemon stcode ships**, TUI included. Under
`full-auto` nothing ever *asks* — `requires_approval` returns False for every permission
— so an attached human is a spectator. The parameter exists for a future policy object
that really would answer `approval_request`.

It raises `AutonomyRefused`, not `SystemExit`: the TUI creates sessions inside a worker
task, which swallows `SystemExit` and shows the user nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

from stcode.core.harness.approvals import ApprovalMode

SANDBOX_ENV = "STCODE_SANDBOX"
"""Set to 1 in the image (§9.4). The escape hatch for a container runtime we cannot
otherwise detect — gVisor, a rootless namespace, CI — and the reason this is not just a
`/.dockerenv` check. Setting it on your laptop is lying to yourself, not to us."""

CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
_CGROUP_MARKERS = ("docker", "lxc", "kubepods", "containerd", "podman", "libpod")

REFUSAL = (
    "full-auto with no approver means the agent runs arbitrary commands with your "
    "privileges and nothing is asked first. Run it in a container (set "
    f"{SANDBOX_ENV}=1 there), or use --mode auto-edit. There is no override flag."
)


class AutonomyRefused(Exception):
    """`full-auto` was requested somewhere its blast radius is not contained."""


def in_container() -> bool:
    """Whether this process looks contained: the env flag, a runtime marker file, or
    pid 1's cgroup naming a container runtime."""
    if os.environ.get(SANDBOX_ENV, "").strip().lower() in ("1", "true", "yes"):
        return True
    if any(Path(marker).exists() for marker in CONTAINER_MARKERS):
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in cgroup for marker in _CGROUP_MARKERS)


def guard_autonomy(mode: ApprovalMode, has_approver: bool = False) -> None:
    """Refuse `full-auto` outside a container when nothing will approve anything."""
    if mode != "full-auto" or has_approver:
        return
    if not in_container():
        raise AutonomyRefused(REFUSAL)


__all__ = [
    "CONTAINER_MARKERS",
    "REFUSAL",
    "SANDBOX_ENV",
    "AutonomyRefused",
    "guard_autonomy",
    "in_container",
]
