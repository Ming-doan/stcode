"""
Search tools — `glob`, `grep`, `ls`.

Shell out to `ripgrep` and `fd`; do not hand-roll. Not just for speed — `.gitignore`
semantics, binary detection, encoding recovery and PCRE2 are each a project's worth of
edge cases, and a Python reimplementation gets them wrong in ways that surface as an
agent confidently reporting a symbol does not exist.

`ripgrep` is a declared dependency (a wheel drops `rg` next to the interpreter). `fd`
has no wheel, so `glob` degrades to `rg --files`, then to `pathlib`. A missing binary
should cost speed, not the tool.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated, Literal, Sequence

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import DEFAULT_IGNORED_DIRS, HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool

GREP_MAX_OUTPUT = 16384
"""`output_mode="content"` is the one search that legitimately returns prose. Above this
it is elided into `tool_out`, where the agent can filter it in Python."""

_SUBPROCESS_TIMEOUT = 60.0


async def run_capture(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = _SUBPROCESS_TIMEOUT
) -> tuple[int, str, str]:
    """Run a command and capture it. Shared by every tool here.

    `create_subprocess_exec`, never `_shell`: these argv lists hold agent-authored
    patterns, and a pattern containing a backtick would run a command.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{Path(argv[0]).name} is not installed or not on PATH.") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ToolError(
            f"{Path(argv[0]).name} did not finish within {timeout:.0f}s. Narrow the search."
        ) from None

    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def find_binary(name: str) -> str | None:
    """Locate a helper binary on PATH, or next to the interpreter.

    The second half is what makes the bundled ripgrep work: a wheel-installed binary
    lands beside `python` in the virtualenv, which is on PATH when it is activated and
    not when something invokes `.venv/bin/python` directly.
    """
    found = shutil.which(name)
    if found:
        return found
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() and os.access(local, os.X_OK) else None


def _have(binary: str) -> bool:
    return find_binary(binary) is not None


# ---- glob ----------------------------------------------------------------------


@tool(permission=ToolPermission.READ)
async def glob(
    pattern: str,
    path: str = ".",
    limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Find files by name pattern, most recently modified first.

    Fast on any repository size. Use it when you know something about a file's name or
    extension and not its contents — `grep` is the tool for the other direction.

    Results are ordered by modification time, newest first, which is usually the order
    you want: the files someone has been working in are the files a task is about.

    Args:
        pattern: Glob pattern, e.g. `**/*.py`, `src/**/test_*.ts`, `Dockerfile*`.
        path: Directory to search in. Defaults to the working directory.
        limit: Maximum paths to return.
    """
    context = runtime.context
    root = context.resolve(path)
    if not root.is_dir():
        raise ToolError(f"{root} is not a directory.")

    matches = await _glob_paths(pattern, root, context)
    if not matches:
        return f"No files matching {pattern!r} under {root}."

    # Stat once, sort once. Files can vanish between listing and stat in a live repo.

    def mtime(candidate: Path) -> float:
        try:
            return candidate.stat().st_mtime
        except OSError:
            return 0.0

    matches.sort(key=mtime, reverse=True)
    shown = matches[:limit]
    body = "\n".join(str(match) for match in shown)
    if len(matches) > limit:
        body += f"\n\n[{len(matches):,} matches, showing the {limit} most recent]"
    return body


# Flags for each lister, in preference order; each is called as FLAGS + [pattern, "."].
# fd's `--glob` matches the whole pattern instead of treating it as a regex, and
# `--full-path` is what lets `src/**/*.py` mean what it says.
_LISTERS = (
    ("fd", ("--glob", "--full-path", "--type", "f", "--hidden", "--exclude", ".git")),
    ("rg", ("--files", "--hidden", "--glob", "!.git", "--glob")),
)


async def _glob_paths(pattern: str, root: Path, context: HarnessContext) -> list[Path]:
    for name, flags in _LISTERS:
        binary = find_binary(name)
        if binary is None:
            continue
        code, out, err = await run_capture([binary, *flags, pattern, "."], cwd=root)
        if code not in (0, 1):
            raise ToolError(f"{name} failed: {err.strip()}")
        return [root / line for line in out.splitlines() if line]

    # Neither binary present. Correct, just slower, and it does its own ignoring.
    return [
        candidate
        for candidate in root.glob(pattern)
        if candidate.is_file() and not _ignored(candidate, root)
    ]


def _ignored(candidate: Path, root: Path) -> bool:
    return any(part in DEFAULT_IGNORED_DIRS for part in candidate.relative_to(root).parts)


# ---- grep ----------------------------------------------------------------------


@tool(permission=ToolPermission.READ, max_output=GREP_MAX_OUTPUT)
async def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    type: str | None = None,
    output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
    case_insensitive: bool = False,
    context_lines: Annotated[int, Field(ge=0, le=20)] = 0,
    multiline: bool = False,
    head_limit: Annotated[int | None, Field(ge=1)] = None,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Search file contents with a regular expression. Respects `.gitignore`.

    This is a ripgrep front end, so `pattern` is Rust regex syntax — `\\b`, `\\d`, and
    lookaround-free assertions all work; PCRE backreferences do not. Literal braces and
    parentheses need escaping (`\\{`, `\\(`).

    Start with the default `files_with_matches` to find out *where* something lives,
    then re-run with `output_mode="content"` on a narrower path. Asking for content
    across a whole repository is how a search turns into a context-window problem.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search. Defaults to the working directory.
        glob: Only search files matching this glob, e.g. `*.py`, `**/*.{ts,tsx}`.
        type: Only search this ripgrep file type, e.g. `py`, `rust`, `js`. Faster
            than `glob` when it fits.
        output_mode: `files_with_matches` lists paths, `content` shows matching lines,
            `count` reports how many matches each file has.
        case_insensitive: Match without regard to case.
        context_lines: Lines of context to show either side of a match. Only meaningful
            with `output_mode="content"`.
        multiline: Let the pattern span line breaks, so `.` matches newlines too.
        head_limit: Keep only the first N results.
    """
    context = runtime.context
    target = context.resolve(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist.")
    if not _have("rg"):
        raise ToolError(
            "ripgrep (`rg`) could not be found, though it is a declared dependency of "
            "stcode. Reinstall the environment (`uv sync`), or put `rg` on PATH."
        )

    argv = [find_binary("rg") or "rg", "--no-messages"]
    if output_mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif output_mode == "count":
        argv.append("--count-matches")
    else:
        argv += ["--line-number", "--with-filename"]
        if context_lines:
            argv += ["--context", str(context_lines)]
    if case_insensitive:
        argv.append("--ignore-case")
    if multiline:
        argv += ["--multiline", "--multiline-dotall"]
    if glob:
        argv += ["--glob", glob]
    if type:
        argv += ["--type", type]
    argv += ["--regexp", pattern, str(target)]

    code, out, err = await run_capture(argv, cwd=context.cwd)
    if code not in (0, 1):
        raise ToolError(f"rg failed: {err.strip() or f'exit {code}'}")
    if not out.strip():
        hint = "" if output_mode == "content" else " Try `output_mode=\"content\"` for a wider view."
        return f"No matches for {pattern!r} in {target}.{hint}"

    lines = out.splitlines()
    total = len(lines)
    if head_limit is not None:
        lines = lines[:head_limit]
    body = "\n".join(lines)
    if head_limit is not None and total > head_limit:
        body += f"\n\n[{total:,} results, showing first {head_limit}]"
    return body


# ---- ls ------------------------------------------------------------------------


@tool(permission=ToolPermission.READ)
async def ls(
    path: str = ".",
    ignore: list[str] | None = None,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """List the contents of a directory, skipping anything `.gitignore` excludes.

    One level only. Use `glob` to look deeper, and prefer it whenever you already know
    what you are looking for — listing a tree to find a file you could have matched by
    name spends a lot of context on directory names.

    Args:
        path: Directory to list. Defaults to the working directory.
        ignore: Extra glob patterns to skip, e.g. `["*.lock", "snapshots"]`.
    """
    context = runtime.context
    target = context.resolve(path)
    if not target.exists():
        raise ToolError(f"{target} does not exist.")
    if not target.is_dir():
        raise ToolError(f"{target} is a file, not a directory. Use `read` to see it.")

    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError as exc:
        raise ToolError(f"Cannot list {target}: {exc.strerror}.") from exc

    ignored = await _gitignored(entries, target, context)
    patterns = tuple(ignore or ())

    rows: list[str] = []
    hidden = 0
    for entry in entries:
        if entry in ignored or entry.name in DEFAULT_IGNORED_DIRS:
            hidden += 1
            continue
        if any(entry.match(pattern) for pattern in patterns):
            hidden += 1
            continue
        rows.append(_describe(entry))

    if not rows:
        return f"{target} is empty." if not hidden else f"{target} has {hidden} entries, all ignored."
    footer = f"\n\n[{hidden} ignored entries hidden]" if hidden else ""
    return f"{target}\n" + "\n".join(rows) + footer


def _describe(entry: Path) -> str:
    if entry.is_dir():
        return f"  {entry.name}/"
    try:
        size = entry.stat().st_size
    except OSError:
        return f"  {entry.name}"
    return f"  {entry.name}  ({_human_size(size)})"


def _human_size(size: int) -> str:
    scaled = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if scaled < 1024 or unit == "GB":
            return f"{scaled:.0f}B" if unit == "B" else f"{scaled:.1f}{unit}"
        scaled /= 1024
    return f"{scaled:.1f}GB"


async def _gitignored(entries: list[Path], target: Path, context: HarnessContext) -> set[Path]:
    """Ask git which entries are ignored — one subprocess, not one per entry.

    `git check-ignore` is the only thing that reads `.gitignore` the way git does:
    nested files, negations, `core.excludesFile`, `.git/info/exclude`. Outside a repo it
    exits non-zero and we fall back to `DEFAULT_IGNORED_DIRS`.
    """
    if not entries or not _have("git"):
        return set()
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "check-ignore", "--stdin",
            cwd=str(target),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, NotImplementedError):
        return set()

    payload = "\n".join(entry.name for entry in entries).encode()
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(payload), 10.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return set()

    names = {line for line in stdout.decode("utf-8", errors="replace").splitlines() if line}
    return {entry for entry in entries if entry.name in names}


__all__ = ["find_binary", "glob", "grep", "ls", "run_capture"]
