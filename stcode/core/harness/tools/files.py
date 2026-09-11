"""
File tools — `read`, `write`, `edit`.

One design, not three: **an agent may only change what it has looked at.** `read`
records what it saw; `write` and `edit` refuse a file this session has not read, and
warn when it changed underneath them. The cost is asymmetric — a refused edit costs one
turn, an overwritten file the agent never read costs work that no longer exists.

`edit` matches an exact, unique string, not a line range or a diff: line numbers go
stale the moment anything above them moves, and a fuzzy match silently edits the wrong
place, while a unique literal either matches once or fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool

MAX_LINE_LENGTH = 2000
"""Chars per line before it is cut. A minified bundle is one line of hundreds of
thousands of chars, and without this a single `read` blows the whole context window."""

DEFAULT_READ_LIMIT = 2000
"""Lines returned when the caller does not say. Enough for almost any source file, small
enough that reading a log by accident is survivable."""

READ_MAX_OUTPUT = 32768
"""`read` shows more than other tools before eliding — file contents are the ground
truth an agent reasons from, and a half-seen function is worse than a slow turn."""

_BINARY_SNIFF_BYTES = 8192


def _is_binary(path: Path) -> bool:
    """A NUL byte in the first pages — what `grep` and `git` use, and right about source
    trees far more often than any extension list."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False


def number_lines(lines: list[str], start: int = 1) -> str:
    """Render with line numbers, `cat -n` style.

    Not decoration: they are how the agent cites `file.py:42`, how `edit` failures are
    explained, and how a second read lines up with the first.
    """
    return "\n".join(f"{start + offset:6d}\t{line}" for offset, line in enumerate(lines))


@tool(permission=ToolPermission.READ, max_output=READ_MAX_OUTPUT)
async def read(
    path: str,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int | None, Field(ge=1)] = None,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Read a file from the filesystem and return it with line numbers.

    Prefer reading a whole file over guessing which part matters: the numbers you get
    back are what you cite in your answer and what `edit` needs to be unambiguous. For
    a file too large to take in one go, page through it with `offset`.

    Reading a file is also what earns you the right to change it — `write` and `edit`
    both refuse to touch a file this session has not read.

    Args:
        path: File to read. Absolute, or relative to the session's working directory.
        offset: 0-based line to start at. Use when paging through a large file.
        limit: How many lines to return. Defaults to 2000.
    """
    context = runtime.context
    target = context.resolve(path)

    if not target.exists():
        near = _suggest_neighbours(target)
        raise ToolError(f"{target} does not exist.{near}")
    if target.is_dir():
        raise ToolError(f"{target} is a directory. Use `ls` to list it, or `glob` to find files in it.")
    if _is_binary(target):
        size = target.stat().st_size
        raise ToolError(
            f"{target} is a binary file ({size:,} bytes) and cannot be read as text. "
            "Use `bash` with a tool that understands its format."
        )

    text = target.read_text(encoding="utf-8", errors="replace")
    context.note_read(target)

    if not text:
        return f"{target} exists but is empty (0 bytes)."

    lines = text.splitlines()
    window = lines[offset : offset + (limit or DEFAULT_READ_LIMIT)]
    if not window:
        raise ToolError(
            f"{target} has {len(lines):,} lines; offset {offset} is past the end."
        )

    clipped = [
        line if len(line) <= MAX_LINE_LENGTH else f"{line[:MAX_LINE_LENGTH]}… [line truncated]"
        for line in window
    ]
    body = number_lines(clipped, start=offset + 1)

    shown_to = offset + len(window)
    if shown_to < len(lines):
        body += (
            f"\n\n[showing lines {offset + 1}-{shown_to} of {len(lines):,}; "
            f"continue with offset={shown_to}]"
        )
    return body


@tool(permission=ToolPermission.WRITE)
async def write(
    path: str,
    content: str,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Write `content` to `path`, replacing whatever is there. Creates parent directories.

    This replaces the entire file. To change part of one, use `edit` — a whole-file
    rewrite of a file you only partly understand silently drops the parts you did not
    reproduce.

    Never write documentation, READMEs, or summary files unless they were asked for.

    Args:
        path: File to write. Absolute, or relative to the working directory.
        content: The complete new contents of the file.
    """
    context = runtime.context
    target = context.ensure_writable(path)

    existed = target.exists()
    if existed:
        if target.is_dir():
            raise ToolError(f"{target} is a directory.")
        if not context.has_read(target):
            raise ToolError(
                f"{target} already exists and this session has not read it. Read it "
                "first — overwriting a file you have not seen destroys work you cannot "
                "get back."
            )
        if context.changed_since_read(target):
            raise ToolError(
                f"{target} changed on disk since you read it. Read it again, then decide "
                "whether your change still applies."
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    context.note_read(target)

    verb = "Updated" if existed else "Created"
    return f"{verb} {target} ({len(content.splitlines()):,} lines, {len(content):,} bytes)."


@tool(permission=ToolPermission.WRITE)
async def edit(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Replace an exact string in a file. The match must be unique unless `replace_all`.

    Include enough surrounding context in `old_string` to make it unique — usually two
    or three lines is plenty. If the edit fails because the string appears more than
    once, widen it rather than switching to `replace_all`: "there are three of these and
    I meant the second" is a different intent from "change all three".

    Preserve the file's existing indentation exactly. `read` prefixes every line with a
    line number and a tab; that prefix is not part of the file, and including it in
    `old_string` is the most common reason an edit finds no match.

    Args:
        path: File to edit.
        old_string: Exact text to find, including its indentation.
        new_string: Text to replace it with. Must differ from `old_string`.
        replace_all: Replace every occurrence instead of requiring exactly one.
    """
    context = runtime.context
    target = context.ensure_writable(path)

    if old_string == new_string:
        raise ToolError("`old_string` and `new_string` are identical — nothing to do.")
    if not target.exists():
        raise ToolError(f"{target} does not exist. Use `write` to create it.")
    if not context.has_read(target):
        raise ToolError(f"Read {target} before editing it.")
    if context.changed_since_read(target):
        raise ToolError(
            f"{target} changed on disk since you read it. Read it again before editing."
        )

    original = target.read_text(encoding="utf-8", errors="replace")
    occurrences = original.count(old_string)

    if occurrences == 0:
        raise ToolError(
            f"`old_string` was not found in {target}.\n"
            f"{_mismatch_hint(original, old_string)}"
        )
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"`old_string` appears {occurrences} times in {target}. Add surrounding "
            "lines until it is unique, or pass replace_all=True if you mean all of them."
        )

    updated = original.replace(old_string, new_string)
    target.write_text(updated, encoding="utf-8")
    context.note_read(target)

    count = f"{occurrences} occurrences" if replace_all and occurrences > 1 else "1 occurrence"
    return f"Edited {target} ({count} replaced).\n\n{_edit_preview(updated, new_string)}"


def _edit_preview(text: str, needle: str, context_lines: int = 4) -> str:
    """Show the edited region with line numbers, so the agent sees what it produced.

    A bare confirmation invites the agent to assume the edit landed as imagined. This is
    how a wrong-but-successful edit gets noticed now instead of three turns later.
    """
    index = text.find(needle.split("\n", 1)[0]) if needle else -1
    if index < 0:
        return ""
    line_number = text.count("\n", 0, index)
    lines = text.splitlines()
    start = max(line_number - context_lines, 0)
    end = min(line_number + needle.count("\n") + context_lines + 1, len(lines))
    return number_lines(lines[start:end], start=start + 1)


def _mismatch_hint(text: str, needle: str) -> str:
    """Say *why* a match failed when the reason is recoverable in one step."""
    stripped = "\n".join(line.strip() for line in needle.splitlines())
    haystack = "\n".join(line.strip() for line in text.splitlines())
    if stripped and stripped in haystack:
        return (
            "The text is present but its indentation differs. Read the file again and "
            "copy the leading whitespace exactly."
        )
    first = needle.split("\n", 1)[0].strip()
    if first and first in text:
        return (
            f"Its first line ({first[:60]!r}) is present, so the mismatch is further "
            "down — a trailing space, a tab, or a line ending. Re-read that region."
        )
    return "Read the file and copy the target text from what you see."


def _suggest_neighbours(target: Path, limit: int = 5) -> str:
    """A missing file is usually a typo or the wrong directory. Say which."""
    parent = target.parent
    if not parent.is_dir():
        return f" Its parent directory {parent} does not exist either."
    siblings = sorted(p.name for p in parent.iterdir() if not p.name.startswith("."))
    if not siblings:
        return ""
    return f" {parent} contains: {', '.join(siblings[:limit])}" + (
        f", … ({len(siblings)} entries)" if len(siblings) > limit else ""
    )


__all__ = ["edit", "number_lines", "read", "write"]
