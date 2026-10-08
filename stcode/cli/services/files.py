"""Bounded local workspace discovery; call from a thread worker."""

import os
import subprocess
from pathlib import Path

FILE_LIMIT = 2000


def list_files(root: Path, limit: int = FILE_LIMIT) -> list[str]:
    """Workspace paths for `@`, relative to `root`, newest convention first: git."""
    try:
        finished = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if finished.returncode == 0 and finished.stdout.strip():
            return sorted(finished.stdout.split("\n"))[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    return _walk(root, limit)


def _walk(root: Path, limit: int) -> list[str]:
    """Every file under `root`, dot-directories skipped and bounded.

    Bounded because this runs on a directory nobody vetted: a home directory, or a
    workspace with a 40,000-file build output in it, must not turn typing `@` into a
    minute of walking.
    """
    found: list[str] = []
    for current, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            found.append(str(Path(current, filename).relative_to(root)))
            if len(found) >= limit:
                return sorted(found)
    return sorted(found)
