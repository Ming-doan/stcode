"""
PyREPL — the parent half of the REPL. Owns one `python -u` subprocess.

    repl = PyREPL(cwd=project_root)
    result = await repl.execute("x = load()\nprint(len(x))")
    await repl.inject({"read_ab12": "...the elided remainder..."})
    await repl.aclose()

Why a subprocess and not `exec()` in this process: a model that writes `while True:`
would otherwise hang the whole daemon, and in a container nobody is there to press
Ctrl-C. Here it costs one SIGINT.

Three rules this class keeps:

* **One cell at a time.** A lock, because the namespace is shared state and two
  concurrent cells in it is a race with no upside.
* **Start on first use.** Most sessions never call `repl`. Spawning a Python for them
  is pure waste, so `start()` happens on the first `execute`.
* **A dead worker is replaced, not mourned.** If it stops answering it is killed and
  the next call gets a fresh one — with an empty namespace, which the caller is told.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from stcode.core.common.truncate import DEFAULT_VIEW_LIMIT, NARROW_REQUEST_HINT, elide

WORKER = Path(__file__).with_name("_worker.py")

DEFAULT_TIMEOUT = 120.0
STDERR_TAIL_LINES = 50
"""How much of the worker's stderr is kept for a crash report. Bounded because the
alternative is holding every warning a long session produced."""

INTERRUPT_GRACE = 5.0
"""After SIGINT, how long the worker gets to report back before it is killed. A cell
stuck in a C call cannot be interrupted at all, and waiting forever for it is how a
daemon becomes something you `kill -9` instead of trusting."""

StreamFn = Callable[[str, str], Awaitable[None]]
"""`(stream_name, text) -> None`. Called per line while a cell runs."""


@dataclass
class ExecResult:
    """What one cell produced."""

    ok: bool = True
    outcome: str = "ok"
    """`ok` | `error` | `timeout` | `crashed`. The model needs these apart: a timeout
    means make it smaller, an error means fix it, a crash means the namespace is gone."""

    stdout: str = ""
    error: str | None = None
    duration: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        """Output and traceback, in the order they happened."""
        parts = [part for part in (self.stdout.rstrip("\n"), self.error) if part]
        return "\n".join(parts)

    def view(self, limit: int = DEFAULT_VIEW_LIMIT) -> str:
        """What the model reads. Elided at `limit`, never summarised (rule 1)."""
        return elide(self.text(), limit, hint=NARROW_REQUEST_HINT)


class ReplError(RuntimeError):
    """The worker could not be started or could not be reached."""


class PyREPL:
    """A persistent Python namespace in a subprocess."""

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        python: str = sys.executable,
    ) -> None:
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.env = env or {}
        self.python = python
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        # Injections that arrived before anything started the worker. Held rather than
        # spawning a Python for them: a big tool result is not a reason to pay for an
        # interpreter the session may never use.
        self._pending_injects: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ---- lifecycle ----

    async def start(self) -> None:
        """Spawn the worker. Idempotent."""
        if self.running:
            return
        environment = {**os.environ, **self.env}
        # The generated MCP stubs live under the workspace, so the workspace has to be
        # importable from inside a cell. This is what makes `from mcp_servers.github
        # import list_issues` work (step 8).
        roots = [str(self.cwd), str(self.cwd / ".stcode")]
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join([*roots, existing]) if existing else os.pathsep.join(roots)

        try:
            self._process = await asyncio.create_subprocess_exec(
                self.python, "-u", str(WORKER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                env=environment,
            )
        except OSError as exc:
            raise ReplError(f"Could not start the REPL worker: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="stcode-repl-stderr")

        if self._pending_injects:
            values, self._pending_injects = self._pending_injects, {}
            await self._request({"type": "inject", "values": values})

    async def _drain_stderr(self) -> None:
        """Keep the worker's stderr pipe empty, and keep the last of it.

        Two reasons, and the second is the one that bites: a subprocess started from a
        cell inherits this pipe, and a server that logs freely into a pipe nobody reads
        will block on a full buffer and hang the cell that started it.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip("\n"))

    async def aclose(self) -> None:
        """Close stdin, then kill if it does not leave."""
        process = self._process
        self._process = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def __aenter__(self) -> "PyREPL":
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # ---- the two verbs ----

    async def execute(
        self,
        code: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_stream: StreamFn | None = None,
    ) -> ExecResult:
        """Run one cell and return what it printed."""
        async with self._lock:
            await self.start()
            started = asyncio.get_running_loop().time()
            request_id = uuid.uuid4().hex[:8]
            try:
                reply = await self._exchange(
                    {"type": "exec", "id": request_id, "code": code},
                    timeout=timeout,
                    on_stream=on_stream,
                )
            except asyncio.TimeoutError:
                return await self._after_timeout(request_id, timeout, started, on_stream)
            except ReplError as exc:
                await self.aclose()
                return ExecResult(
                    ok=False,
                    outcome="crashed",
                    error=f"{exc}\nThe namespace is gone; the next cell starts a fresh one.",
                    duration=asyncio.get_running_loop().time() - started,
                )

        return _result_from(reply, asyncio.get_running_loop().time() - started)

    async def inject(self, values: dict[str, Any]) -> None:
        """Push values into the cell namespace's `tool_out`.

        This is the bridge rule 1 depends on: what `elide` cut from a tool result is
        parked here, so the hint that names `tool_out["..."]` is telling the truth.
        """
        if not values:
            return
        if not self.running:
            self._pending_injects.update(values)
            return
        async with self._lock:
            with contextlib.suppress(ReplError, asyncio.TimeoutError):
                await self._exchange({"type": "inject", "values": values}, timeout=10.0)

    async def interrupt(self) -> None:
        """SIGINT the cell in flight. The namespace survives."""
        if self.running and self._process is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self._process.send_signal(signal.SIGINT)

    # ---- wire ----

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        return await self._exchange(message, timeout=10.0)

    async def _exchange(
        self,
        message: dict[str, Any],
        *,
        timeout: float | None,
        on_stream: StreamFn | None = None,
    ) -> dict[str, Any]:
        """Send one message, read until its answer. Forwards `chunk` lines on the way."""
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise ReplError("the REPL worker is not running")

        message.setdefault("id", uuid.uuid4().hex[:8])
        try:
            process.stdin.write((json.dumps(message, default=str) + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (ConnectionError, OSError, RuntimeError) as exc:
            raise ReplError(f"the REPL worker stopped listening: {exc}") from exc

        wanted = str(message["id"])
        if timeout is None:
            return await self._read_frame(on_stream=on_stream, wanted=wanted)
        return await asyncio.wait_for(
            self._read_frame(on_stream=on_stream, wanted=wanted), timeout
        )

    async def _read_frame(
        self, *, on_stream: StreamFn | None = None, wanted: str | None = None
    ) -> dict[str, Any]:
        """Read until a real answer arrives. Chunks are forwarded, not returned.

        `wanted` matches on the request id. Without it, one late frame — the result of
        a cell that answered its SIGINT after we stopped waiting — would be handed to
        the *next* call, and every reply after that would be off by one.
        """
        process = self._process
        if process is None or process.stdout is None:
            raise ReplError("the REPL worker is not running")
        while True:
            line = await process.stdout.readline()
            if not line:
                raise ReplError(await self._death_note(process))
            try:
                frame = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "chunk":
                if on_stream is not None:
                    await on_stream("stdout", str(frame.get("text", "")))
                continue
            if wanted and str(frame.get("id", "")) != wanted:
                continue  # a stale answer to a call that already gave up
            return frame  # type: ignore[no-any-return]

    async def _after_timeout(
        self, request_id: str, timeout: float, started: float, on_stream: StreamFn | None
    ) -> ExecResult:
        """Interrupt, then give the worker a moment to report what it had.

        A cell that answers the SIGINT keeps its namespace, which is the whole reason to
        try this before reaching for the kill. We wait for the interrupted cell's *own*
        result frame — sending anything else here would leave that frame in the pipe for
        the next call to trip over.
        """
        await self.interrupt()
        elapsed = asyncio.get_running_loop().time() - started
        try:
            reply = await asyncio.wait_for(
                self._read_frame(on_stream=on_stream, wanted=request_id), INTERRUPT_GRACE
            )
        except (asyncio.TimeoutError, ReplError):
            await self.aclose()
            return ExecResult(
                ok=False,
                outcome="timeout",
                error=(
                    f"The cell exceeded {timeout:.0f}s and would not stop, so the "
                    "interpreter was killed. The namespace is gone. Split the work into "
                    "smaller cells."
                ),
                duration=elapsed,
            )
        return ExecResult(
            ok=False,
            outcome="timeout",
            stdout=str(reply.get("stdout", "")),
            error=(
                f"The cell was interrupted after {timeout:.0f}s. The namespace survived, "
                "so whatever it managed to build is still there to inspect."
            ),
            duration=elapsed,
        )

    async def _death_note(self, process: asyncio.subprocess.Process) -> str:
        """Whatever the worker said on its way out — usually the real reason."""
        detail = "\n".join(self._stderr_tail).strip()
        code = process.returncode
        return f"the REPL worker exited ({code}){': ' + detail[-800:] if detail else ''}"

    def __repr__(self) -> str:
        return f"PyREPL(cwd={self.cwd}, running={self.running})"


def _result_from(reply: dict[str, Any], duration: float) -> ExecResult:
    ok = bool(reply.get("ok", False))
    return ExecResult(
        ok=ok,
        outcome="ok" if ok else "error",
        stdout=str(reply.get("stdout", "")),
        error=reply.get("error"),
        duration=duration,
    )


__all__ = ["DEFAULT_TIMEOUT", "ExecResult", "PyREPL", "ReplError", "StreamFn"]
