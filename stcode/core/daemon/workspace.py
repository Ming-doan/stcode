"""Daemon workspace operations for explicit client requests."""

import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

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
            return sorted(set(finished.stdout.splitlines()))[:limit]
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


async def run_shell(command: str, cwd: Path, timeout: float) -> dict[str, Any]:
    """Run a user command in the session workspace; reap its group on timeout/close."""
    timeout = max(1.0, min(120.0, timeout))
    process = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    # Drain continuously but bound the retained output, including single huge lines.
    output = bytearray()
    truncated = False

    async def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            remaining = max(0, 1024 * 1024 - len(output))
            output.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        await process.wait()

    pump = asyncio.create_task(drain())
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(pump), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        if not pump.done():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await pump
    text = output.decode("utf-8", errors="replace").rstrip()
    if truncated:
        text += "\n… output truncated at 1 MiB"
    return {"output": text, "exit_code": 124 if timed_out else process.returncode,
            "timed_out": timeout if timed_out else None}
