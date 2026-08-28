"""
Snapshot — interface for saving and restoring a kernel namespace across a restart.

Needed by phase 3 ("child survives parent compaction"), not before: a restart today
loses the namespace, which is the honest behaviour, and a half-working restore is worse
than none because the agent cannot tell which variables came back.

Design notes for whoever implements it:

- `pickle` will not do. Live objects — open files, sockets, provider clients, kernel
  handles — are most of what a long session accumulates. `dill` handles more of it and
  would be a new dependency.
- Save must be best-effort per name, never all-or-nothing, and must report what it
  dropped. A restored namespace that silently lost three variables produces an agent
  confidently reasoning about state that is gone.
- Names starting with `_` and the bootstrap contract itself are excluded; bootstrap
  re-runs on restart and rebuilds them.
"""

from __future__ import annotations

from pathlib import Path


class SnapshotUnavailable(NotImplementedError):
    """Raised until namespace snapshotting lands in phase 3."""


async def save(kernel: object, path: str | Path) -> list[str]:
    """Persist the kernel's picklable globals; return the names that could not be saved."""
    raise SnapshotUnavailable("kernel snapshots land in phase 3")


async def restore(kernel: object, path: str | Path) -> list[str]:
    """Reload a snapshot into a fresh kernel; return the names restored."""
    raise SnapshotUnavailable("kernel snapshots land in phase 3")
