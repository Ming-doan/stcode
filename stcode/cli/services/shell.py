"""Local shell I/O. Results never enter a daemon session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from .preferences import clamp_shell_timeout


@dataclass(frozen=True)
class ShellResult:
    output: str = ""
    exit_code: int = 0
    timed_out: float | None = None
    error: Exception | None = None


def run_shell(command: str, cwd: Path, timeout: float) -> ShellResult:
    """Run in the terminal's workspace; the caller must use a thread worker."""
    timeout = clamp_shell_timeout(timeout)
    try:
        finished = subprocess.run(
            command,
            shell=True,
            cwd=cwd if cwd.is_dir() else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(exit_code=124, timed_out=timeout)
    except (OSError, ValueError) as exc:
        return ShellResult(exit_code=1, error=exc)
    parts = [part for part in (finished.stdout, finished.stderr) if part.strip()]
    return ShellResult("\n".join(part.rstrip() for part in parts), finished.returncode)
