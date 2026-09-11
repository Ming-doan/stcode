"""
Shell tools — `bash`, and `bash_output` for the background case.

Commands run with the user's privileges; containerize before any autonomous run. From
inside the process this module does four things:

* **Refuses the unrecoverable** — a short denylist of damage no later turn could undo.
  Not a security boundary (a script or base64 goes round it); it guards against a model
  writing something destructive by accident.
* **Asks proportionally.** `git status` and `rm -rf build/` are the same tool call, so
  read-only commands drop to READ per call and the rest still ask.
* **Never hangs.** stdin closed, pagers off, prompts disabled. A command that stops to
  ask a question stops the turn and produces nothing to react to.
* **Each call is a fresh process** — `cd`, `export` and `source` do not carry over. Pass
  `cwd` instead. The model will assume otherwise, so the docstring says so outright.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 900.0
BASH_MAX_OUTPUT = 16384

NON_INTERACTIVE_ENV = {
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "DEBIAN_FRONTEND": "noninteractive",
    "CI": "1",
    "NO_COLOR": "1",
    "TERM": "dumb",
}
"""Set on every command. `TERM=dumb` and `NO_COLOR` matter as much as the prompt
suppressors: ANSI escapes are invisible to the reader and expensive in the context."""

_DESTRUCTIVE = (
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\b[^|;]*\bof=/dev/"), "raw write to a block device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "redirect onto a block device"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;?\s*:"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "host power control"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|fi)?sh\b"), "piping a download into a shell"),
    (re.compile(r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+777\s+/\s*$"), "world-writable root"),
    (re.compile(r"\bgit\s+(push\s+[^|;]*--force(?!-with-lease)|reset\s+--hard\s+\S*origin)"), "irreversible git history rewrite"),
)
"""Refused outright. The test for belonging here is "could the next turn undo it?", not
"is it dangerous?". Deletion is judged by target instead, since `rm -rf build/` is
fine."""

_IRREPLACEABLE = frozenset(
    {
        "/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/*", "${HOME}", "..", "../", "../*",
        ".", "./", "./*", "*",
        "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc",
        "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
    }
)
"""Delete targets with nothing behind them. `.` and `*` are here because from the
session's cwd they mean the user's whole project."""


def _deletes_something_irreplaceable(command: str) -> str | None:
    """Whether any `rm` here points at a path that cannot be recreated.

    Judged on the target, not the flags: `rm -rf node_modules/` and `rm -rf /` differ
    only in the argument, and refusing on `-rf` would block routine cleanup until
    whoever reads the refusals learns to ignore them.
    """
    for segment in _split_command(command):
        if os.path.basename(segment[0]) != "rm":
            continue
        for argument in segment[1:]:
            if argument.startswith("-"):
                continue
            normalized = argument.rstrip("/") + "/" if argument.endswith("/") else argument
            if argument in _IRREPLACEABLE or normalized in _IRREPLACEABLE:
                return f"deleting {argument!r}, which is not something a later turn could rebuild"
    return None


_INTERACTIVE = (
    (re.compile(r"\bgit\s+(rebase|add|commit)\b[^|;]*\s-i\b"), "git's interactive mode"),
    (re.compile(r"^\s*(vim?|nano|emacs|less|more|top|htop)\b"), "a full-screen program"),
    (re.compile(r"\bssh\b(?![^|;]*\s-o\s*BatchMode)"), "ssh, which may prompt for a passphrase"),
)
"""Commands that wait for a human at a TTY there is no way to reach. Refused with an
alternative rather than left to consume the whole timeout in silence."""

_READ_ONLY_COMMANDS = frozenset(
    {
        "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "tree",
        "echo", "date", "whoami", "hostname", "uname", "which", "type", "env", "printenv",
        "grep", "rg", "fd", "find", "sort", "uniq", "cut", "awk", "sed", "diff", "cmp",
        "python", "python3", "node", "jq", "basename", "dirname", "realpath", "readlink",
    }
)
_READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"status", "log", "diff", "show", "branch", "remote", "config",
                      "blame", "describe", "rev-parse", "ls-files", "shortlog", "tag"}),
    "uv": frozenset({"tree", "version", "python"}),
    "docker": frozenset({"ps", "images", "logs", "version", "inspect"}),
    "npm": frozenset({"ls", "view", "outdated"}),
    "cargo": frozenset({"tree", "metadata"}),
}
"""`sed` and `awk` are here for the overwhelmingly common case, a pipe filter. `sed -i`
edits in place, so `is_read_only` checks for that rather than trusting the name."""


@dataclass
class BackgroundShell:
    """A command still running after `bash` returned.

    Output drains continuously into `buffer` rather than on demand: a process whose pipe
    fills up blocks forever, and a build stalled at 64 KB with no error is close to
    impossible to diagnose from outside.
    """

    id: str
    command: str
    process: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.monotonic)
    buffer: list[str] = field(default_factory=list)
    consumed: int = 0
    pump: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    def drain(self) -> str:
        """Everything printed since the last drain."""
        chunk = "".join(self.buffer[self.consumed :])
        self.consumed = len(self.buffer)
        return chunk


def _split_command(command: str) -> list[list[str]]:
    """Best-effort split into the commands a line runs.

    `shlex` is not a shell parser and this is not trying to be one — it is enough to
    decide whether every segment of `git status | head -20` is read-only, and it fails
    closed: an unparseable line is simply not read-only.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in ("|", "||", "&&", ";", "&"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def is_read_only(command: str) -> bool:
    """Whether every segment of `command` only observes the workspace."""
    segments = _split_command(command)
    if not segments:
        return False
    for segment in segments:
        name = os.path.basename(segment[0])
        if name == "sed" and any(arg.startswith("-i") for arg in segment[1:]):
            return False  # in-place edit wearing a filter's name
        if name in _READ_ONLY_SUBCOMMANDS:
            arguments = [arg for arg in segment[1:] if not arg.startswith("-")]
            if not arguments or arguments[0] not in _READ_ONLY_SUBCOMMANDS[name]:
                return False
            continue
        if name not in _READ_ONLY_COMMANDS:
            return False
    return True


def _refuse(command: str) -> str | None:
    """The reason this command will not be run, or None."""
    for pattern, why in _DESTRUCTIVE:
        if pattern.search(command):
            return (
                f"refused: this looks like {why}, which no later turn could undo. "
                "If you genuinely need it, ask the user to run it themselves."
            )
    deletion = _deletes_something_irreplaceable(command)
    if deletion:
        return (
            f"refused: this is {deletion}. Name the specific subdirectory you mean, or "
            "ask the user to do it themselves."
        )
    for pattern, why in _INTERACTIVE:
        if pattern.search(command):
            return (
                f"refused: this invokes {why} and there is no terminal to answer it. "
                "Use the non-interactive equivalent."
            )
    return None


def _permission_for(arguments: dict[str, object]) -> ToolPermission:
    command = str(arguments.get("command", ""))
    return ToolPermission.READ if is_read_only(command) else ToolPermission.EXECUTE


@tool(
    permission=ToolPermission.EXECUTE,
    permission_for=_permission_for,
    max_output=BASH_MAX_OUTPUT,
)
async def bash(
    command: str,
    timeout: Annotated[float, Field(gt=0, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
    background: bool = False,
    cwd: str = "",
    description: str = "",
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Run a shell command in the session's working directory.

    Runs with your privileges. Prefer the dedicated tools where one fits — `read`,
    `glob`, and `grep` are faster than `cat`, `find`, and `grep`, and their output is
    shaped for you rather than for a terminal. Reach for this when you need a real
    program: a test runner, a build, `git`, a package manager.

    **Every call is a new process.** Nothing carries over between calls — not the
    working directory, not `export`ed variables, not an activated virtualenv. `cd src`
    in one call has no effect on the next one. To work in a subdirectory, pass `cwd`;
    to use a virtualenv, call its binaries by path (`.venv/bin/pytest`) or prefix the
    command (`uv run pytest`).

    The command runs with no stdin and no pager, so anything that would stop to ask a
    question fails instead of hanging. Quote paths containing spaces.

    Use `background=True` for anything long-running you want to keep working alongside —
    a dev server, a watch task, a full test suite. It returns a shell id immediately;
    read from it with `bash_output`.

    Args:
        command: The command line to run.
        timeout: Seconds to wait before giving up and killing it.
        background: Return immediately and keep the command running.
        cwd: Directory to run in, relative to the session root. Defaults to the
            session root itself.
        description: A 5-10 word description of what this does, shown to the user
            while it runs. e.g. "Run the unit tests".
    """
    context = runtime.context
    refusal = _refuse(command)
    if refusal:
        raise ToolError(f"`{command}` was not run — {refusal}")

    working_dir = context.resolve(cwd) if cwd else context.cwd
    if not working_dir.is_dir():
        raise ToolError(f"`{command}` was not run — {working_dir} is not a directory.")

    env = {**context.process_env(), **NON_INTERACTIVE_ENV}
    if description:
        await runtime.progress(description)

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(working_dir),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Its own process group, so a timeout kills the whole pipeline rather than just
        # the shell that spawned it, orphaning the actual work.
        start_new_session=True,
    )

    if background:
        return await _start_background(process, command, context)

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill_group(process)
        await process.wait()
        raise ToolError(
            f"`{command}` was killed after {timeout:.0f}s. Raise `timeout` if it is "
            "genuinely slow, or run it with background=True and poll `bash_output`."
        ) from None

    output = stdout.decode("utf-8", errors="replace").rstrip()
    code = process.returncode or 0
    if code == 0:
        return output or "(no output)"
    # Exit code last: the part the model most needs, and `elide` keeps the tail.
    return f"{output}\n\n[exit {code}]" if output else f"[exit {code}, no output]"


async def _start_background(
    process: asyncio.subprocess.Process, command: str, context: HarnessContext
) -> str:
    shell = BackgroundShell(id=f"sh_{uuid.uuid4().hex[:8]}", command=command, process=process)

    async def pump() -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            shell.buffer.append(line.decode("utf-8", errors="replace"))
        await process.wait()

    shell.pump = asyncio.create_task(pump())
    context.shells[shell.id] = shell
    return (
        f"Started `{command}` in the background as {shell.id} (pid {process.pid}).\n"
        f"Read its output with bash_output({shell.id!r})."
    )


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


@tool(permission=ToolPermission.READ, max_output=BASH_MAX_OUTPUT)
async def bash_output(
    shell_id: str,
    kill: bool = False,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Read new output from a background shell started by `bash(background=True)`.

    Returns only what has been printed since the last time you read it, so polling a
    long build does not re-read the whole log each turn.

    Args:
        shell_id: The id `bash` returned, e.g. `sh_1a2b3c4d`.
        kill: Stop the process after reading whatever it has produced.
    """
    context = runtime.context
    shell = context.shells.get(shell_id)
    if shell is None:
        known = ", ".join(context.shells) or "none"
        raise ToolError(f"No background shell {shell_id!r}. Running: {known}.")

    chunk = shell.drain()
    if kill and shell.running:
        _kill_group(shell.process)
        await shell.process.wait()

    elapsed = time.monotonic() - shell.started_at
    if shell.running:
        status = f"[{shell_id} still running, {elapsed:.0f}s elapsed]"
    else:
        status = f"[{shell_id} exited {shell.process.returncode} after {elapsed:.0f}s]"
        context.shells.pop(shell_id, None)
    return f"{chunk.rstrip()}\n\n{status}" if chunk.strip() else status


__all__ = ["BackgroundShell", "bash", "bash_output", "is_read_only"]
