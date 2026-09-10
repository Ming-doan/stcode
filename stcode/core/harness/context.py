"""
Harness context — the workspace state built-in tools share, and the `T` in `Runtime[T]`.

`Runtime` carries the plumbing of one call (who, when, may-I). This carries what the
*session* knows: where it is rooted, which paths it may write to, what it has already
read, what it plans to do next. Splitting them is what lets a different agent define
its own context and reuse the same `@tool` machinery — see `tools/base.py`.

Two invariants live here rather than in the tools that depend on them, because a rule
enforced in four places is a rule that will soon be enforced in three:

**Path scope.** CLAUDE.md §2.2 rule 4: two sub-agents must never write the same file.
Scope is assigned at spawn and checked in `ensure_writable`, so a sub-agent told to own
`tests/` cannot reach into `src/` no matter what its model decides to try.

**Read before overwrite.** `write` to an existing file requires that this session has
read it first. Clobbering a file the agent never looked at is the single most expensive
mistake a coding agent makes, and it is one the agent cannot detect afterwards — the
old contents are simply gone.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal

from pydantic import BaseModel, Field

from stcode.core.harness.errors import ToolError

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, fine for type checkers
    from stcode.core.harness.skills import SkillRegistry
    from stcode.core.harness.tools.shell import BackgroundShell
    from stcode.core.repl import PyREPL

TodoStatus = Literal["pending", "in_progress", "completed"]

DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        "target",
    }
)
"""Directories skipped when nothing better (a `.gitignore`) is available. Not a security
boundary — a deliberate `read(".venv/...")` still works. It exists so that `ls` and
`glob` answer the question the agent meant to ask."""


# `TodoItem`'s docstring and field descriptions are part of a tool's JSON Schema, so
# they are written *for the model* — short, imperative, no rationale. The rationale is
# here instead: two phrasings rather than one because the UI shows the participle while
# an item runs and the imperative everywhere else, and deriving one from the other in
# English does not survive contact with real task names.
class TodoItem(BaseModel):
    """A single task in the plan."""

    content: str = Field(description="The task, in the imperative: 'Add the retry test'.")
    status: TodoStatus = Field(default="pending", description="Where this task stands.")
    active_form: str = Field(
        default="",
        description="The same task in the present participle, shown while it runs: "
        "'Adding the retry test'.",
    )

    def render(self) -> str:
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}[self.status]
        return f"{mark} {self.content}"


@dataclass
class HarnessContext:
    """What the built-in tools know about this session's workspace."""

    cwd: Path = field(default_factory=Path.cwd)
    """Root for every relative path a tool is given, and the default search root."""

    scope: tuple[Path, ...] = ()
    """Paths this agent may write to. Empty means all of `cwd` — the main agent's
    normal state. A sub-agent gets a narrower tuple at spawn."""

    read_files: dict[Path, float] = field(default_factory=dict)
    """Absolute path -> mtime when this session last read it. Feeds the read-before-
    overwrite check, and lets `edit` notice a file that changed underneath it."""

    todos: list[TodoItem] = field(default_factory=list)
    session_ctx: dict[str, Any] = field(default_factory=dict)
    """Knowledge shared down into sibling sub-agents — the in-process twin of the REPL's
    `session_ctx` (CLAUDE.md §2.1)."""

    shells: dict[str, "BackgroundShell"] = field(default_factory=dict)
    """Background processes started by `bash(..., background=True)`, by shell id."""

    skills: "SkillRegistry | None" = None

    repl: "PyREPL | None" = None
    """The session's persistent Python REPL.

    Attached by `Harness.create`, and lazy: the subprocess is not spawned until the
    first cell runs. Two things read it — the `repl` tool, and `Harness.invoke`, which
    pushes elided payloads into its `tool_out`.

    None for a sub-agent (rule 3: no `task`, no `repl`) and for anything built with
    `load_repl=False`. When it is None the elision hint quietly drops back to "ask for
    a narrower range", because `tool_out` is then not somewhere the model can reach."""

    git: str = ""
    """A one-shot summary of the repository: branch, status, recent commits. Sampled at
    `Harness.create` by `git_context()` and never refreshed, because the system prompt
    is the cached prefix and re-sampling it per turn would invalidate that every turn
    (EXPECTED.md §14 item 2). A turn that needs current state runs `git status`."""

    env: dict[str, str] = field(default_factory=dict)
    """Extra environment for spawned processes, layered over `os.environ`."""

    # ---- paths ----

    def resolve(self, path: str | Path) -> Path:
        """Absolute, `~`-expanded, and rooted at `cwd` when relative.

        Not `Path.resolve()`: that follows symlinks, and a repo checked out under a
        symlinked home would then fail its own scope check for paths the user
        legitimately typed. Only `..` is normalised, which is the part that matters for
        escaping a scope.
        """
        candidate = Path(os.path.expanduser(str(path)))
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        return Path(os.path.normpath(candidate))

    def in_scope(self, path: str | Path) -> bool:
        target = self.resolve(path)
        roots: Iterable[Path] = self.scope or (self.cwd,)
        return any(target == root or root in target.parents for root in roots)

    def ensure_writable(self, path: str | Path) -> Path:
        """Resolve `path` and refuse it if this agent has no business writing there."""
        target = self.resolve(path)
        if self.in_scope(target):
            return target
        allowed = ", ".join(str(p) for p in (self.scope or (self.cwd,)))
        raise ToolError(
            f"{target} is outside this agent's write scope ({allowed}). Another agent "
            "owns that path — report what needs changing there instead of editing it."
        )

    def note_read(self, path: Path) -> None:
        try:
            self.read_files[path] = path.stat().st_mtime
        except OSError:
            self.read_files[path] = 0.0

    def has_read(self, path: Path) -> bool:
        return path in self.read_files

    def changed_since_read(self, path: Path) -> bool:
        """Whether `path` was modified after this session last read it.

        The guard against an agent editing a file from a stale mental model — someone
        else's save, or a sibling agent that should not have been in there at all.
        """
        seen = self.read_files.get(path)
        if seen is None:
            return False
        try:
            return path.stat().st_mtime > seen
        except OSError:
            return False

    # ---- todos ----

    def render_todos(self) -> str:
        if not self.todos:
            return "(no todos)"
        return "\n".join(item.render() for item in self.todos)

    # ---- process environment ----

    def process_env(self) -> dict[str, str]:
        return {**os.environ, **self.env}


class TodoList(BaseModel):
    """Wrapper so `todo_write` takes one well-named argument rather than a bare list."""

    items: list[TodoItem] = Field(default_factory=list)


# ---- repository state ------------------------------------------------------------

GIT_TIMEOUT = 5.0
"""A `git` call that takes longer than this is a repository so large or a filesystem so
slow that blocking session startup on it is the wrong trade. The prompt loses one
section; the agent can still run `git status` itself."""

_GIT_QUERIES = (
    ("Branch", ("rev-parse", "--abbrev-ref", "HEAD")),
    ("Status", ("status", "--short", "--branch")),
    ("Recent commits", ("log", "-5", "--oneline", "--no-decorate")),
)


async def git_context(cwd: Path) -> str:
    """Branch, working-tree status, and the last five commits, as a prompt section.

    Sampled once at `Harness.create` and never refreshed — EXPECTED.md §14 item 2 is
    explicit about this. It is the highest quality-per-line addition to the prompt, and
    re-running it per turn would move the prompt every turn and undo the caching that
    item 1 just bought.

    Not a repository, or `git` not installed: returns "". A missing section is correct
    here, not an error — plenty of work happens outside a checkout.
    """
    async def run(arguments: tuple[str, ...]) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(cwd), *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return ""
        if process.returncode != 0:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()

    if not await run(("rev-parse", "--is-inside-work-tree")):
        return ""

    sections = await asyncio.gather(*(run(arguments) for _, arguments in _GIT_QUERIES))
    lines = ["Repository:"]
    for (label, _), output in zip(_GIT_QUERIES, sections):
        if not output:
            continue
        if "\n" in output:
            lines.append(f"{label}:")
            lines += [f"  {line}" for line in output.splitlines()[:12]]
        else:
            lines.append(f"{label}: {output}")
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "GIT_TIMEOUT",
    "HarnessContext",
    "TodoItem",
    "TodoList",
    "TodoStatus",
    "git_context",
]
