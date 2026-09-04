"""
Invariant 5, as code — `full-auto` without an approver runs only inside a container.

CLAUDE.md §4 rule 5 and EXPECTED.md §11.3. The point of putting it here rather than in
a document is that a document cannot refuse. The daemon calls `guard_autonomy` twice:
once before it binds a socket, and once per session it creates. **There is no override
flag**, and adding one is not a feature request that can be satisfied.

Two details that decide whether this actually holds:

**`has_approver` is False for every daemon stcode ships**, TUI included. It is a real
parameter because a future automated approver — a policy object that answers
`approval_request` without a human — would be one, and the guard should let that
through. But it must not become the override flag by the back door: under `full-auto`
nothing ever *asks*, because `requires_approval` returns False for every permission, so
an attached human is a spectator, not an approver. A TUI that passed `has_approver=True`
would be claiming a safety property it does not have.

**It raises `AutonomyRefused`, not `SystemExit`.** EXPECTED.md sketches `SystemExit`,
which is right for the CLI and wrong one layer down: the TUI creates sessions inside a
worker task, where a `SystemExit` is swallowed by the task and shows the user nothing.
The CLI turns this into an exit; the TUI renders it. The refusal is identical either
way, which is the part the invariant cares about.
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
