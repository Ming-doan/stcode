"""
Python Kernel — a persistent IPython kernel, one per session.

Persistent is the whole point (CLAUDE.md §2.1): variables from turn 3 are still there in
turn 30, so a 400 KB tool result can be sliced later instead of re-fetched, and an
exception costs the cell rather than the session. That rules out `exec()` per turn and
buys, for free, the top-level `await` that `await agent(...)` depends on.

This module owns the Jupyter messaging protocol and nothing else. What the namespace
contains is `bootstrap.py`; what the agent gets to see is `types.ExecResult.view()`.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from jupyter_client.asynchronous.client import AsyncKernelClient
from jupyter_client.manager import AsyncKernelManager

from stcode.core.kernel.bootstrap import BOOTSTRAP_CODE
from stcode.core.kernel.pump import MessagePump
from stcode.core.kernel.types import ExecOutcome, ExecResult, KernelError

DEFAULT_TIMEOUT = 120.0
DEFAULT_STARTUP_TIMEOUT = 60.0
INTERRUPT_GRACE = 10.0
"""After interrupting a timed-out cell, how long to wait for the kernel to report back
before declaring it wedged. A cell stuck in a C extension never returns from SIGINT."""

StreamCallback = Callable[[str, str], Any | Awaitable[Any]]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""Tracebacks arrive colorized. The escapes render as noise and cost tokens."""


class KernelStartupError(RuntimeError):
    """The kernel process failed to start or never became ready."""


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


class PythonKernel:
    """A single session's REPL.

    Use as an async context manager, or call `start()` / `shutdown()` explicitly.
    Executions are serialized: the Jupyter shell channel is FIFO, and interleaving two
    cells over one kernel makes their outputs unattributable.
    """

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        kernel_name: str = "python3",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        env: dict[str, str] | None = None,
        bootstrap: bool = True,
    ) -> None:
        self.cwd = str(cwd) if cwd is not None else os.getcwd()
        self.kernel_name = kernel_name
        self.startup_timeout = startup_timeout
        self.env = env
        self._bootstrap = bootstrap
        self._manager: AsyncKernelManager | None = None
        self._client: AsyncKernelClient | None = None
        self._pump: MessagePump | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------------

    async def is_alive(self) -> bool:
        """Whether the kernel process is still running.

        Async because `AsyncKernelManager.is_alive` is — calling it as a property hands
        back a coroutine object, which is truthy, so a dead kernel reads as alive.
        """
        if self._manager is None:
            return False
        return bool(await self._manager.is_alive())

    async def start(self) -> None:
        if self._manager is not None:
            return

        manager = AsyncKernelManager(kernel_name=self.kernel_name)
        if os.name == "posix":
            # Unix sockets over loopback TCP: no ports to exhaust or collide on, nothing
            # on the wire for another local user to read, and it silences ipykernel's
            # unencrypted-transport warning.
            manager.transport = "ipc"
        # The `python3` kernelspec's argv starts with a bare "python", resolved through
        # PATH. Installed as a tool, that is a different interpreter with no ipykernel —
        # the kernel then dies at startup for reasons nothing in the traceback explains.
        spec = manager.kernel_spec
        if spec is None:
            raise KernelStartupError(f"no kernelspec named {self.kernel_name!r} is installed")
        spec.argv[0] = sys.executable

        # `env=None` is not "inherit" to the provisioner — it dereferences it. Omit the
        # key entirely to get the parent environment.
        extra_env = {"env": self.env} if self.env is not None else {}
        try:
            await manager.start_kernel(cwd=self.cwd, **extra_env)
            client = manager.client()
            client.start_channels()
            await client.wait_for_ready(timeout=self.startup_timeout)
        except Exception as exc:
            await self._teardown(manager, locals().get("client"))
            raise KernelStartupError(f"kernel {self.kernel_name!r} failed to start: {exc}") from exc

        self._manager = manager
        self._client = client
        self._pump = MessagePump(client)
        self._pump.start()

        if self._bootstrap:
            result = await self.execute(BOOTSTRAP_CODE, timeout=self.startup_timeout, silent=True)
            if not result.ok:
                detail = result.error.render() if result.error else result.outcome
                await self.shutdown()
                raise KernelStartupError(f"bootstrap failed: {detail}")

    async def restart(self) -> None:
        """Restart the kernel process, keeping the same ports and re-running bootstrap.

        The namespace does not survive — that is what a restart *is*. Callers holding
        state the session needs must have spilled it to disk first.
        """
        if self._manager is None:
            await self.start()
            return
        if self._pump is not None:
            await self._pump.stop()
        await self._manager.restart_kernel(now=True)
        assert self._client is not None
        await self._client.wait_for_ready(timeout=self.startup_timeout)
        self._pump = MessagePump(self._client)
        self._pump.start()
        if self._bootstrap:
            await self.execute(BOOTSTRAP_CODE, timeout=self.startup_timeout, silent=True)

    async def interrupt(self) -> None:
        """Send SIGINT (or the message-mode equivalent) to the running cell."""
        if self._manager is not None:
            await self._manager.interrupt_kernel()

    async def shutdown(self) -> None:
        manager, client = self._manager, self._client
        self._manager = self._client = None
        if self._pump is not None:
            await self._pump.stop()
            self._pump = None
        await self._teardown(manager, client)

    @staticmethod
    async def _teardown(manager: AsyncKernelManager | None, client: Any) -> None:
        if client is not None:
            try:
                client.stop_channels()
            except Exception:
                pass
        if manager is not None and manager.has_kernel:
            try:
                await manager.shutdown_kernel(now=True)
            except Exception:
                pass

    async def __aenter__(self) -> "PythonKernel":
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.shutdown()

    # ---- execution -------------------------------------------------------------

    async def execute(
        self,
        code: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_stream: StreamCallback | None = None,
        silent: bool = False,
        store_history: bool = True,
    ) -> ExecResult:
        """Run one cell and collect everything it produced.

        `on_stream(name, text)` fires per stream chunk so a UI can render output as it
        arrives — §9's answer to a session that looks hung. It may be sync or async.

        A `timeout` is not a failure of the kernel: the cell is interrupted, the
        resulting KeyboardInterrupt is collected, and the namespace is left intact so
        the agent can inspect whatever the cell managed to build.
        """
        if self._client is None or self._pump is None:
            raise RuntimeError("kernel not started — call start() or use `async with`")
        if not await self.is_alive():
            return ExecResult(outcome=ExecOutcome.DEAD)

        async with self._lock:
            return await self._execute_locked(code, timeout, on_stream, silent, store_history)

    async def _execute_locked(
        self,
        code: str,
        timeout: float,
        on_stream: StreamCallback | None,
        silent: bool,
        store_history: bool,
    ) -> ExecResult:
        assert self._client is not None and self._pump is not None
        started = time.monotonic()

        msg_id: str = self._client.execute(
            code,
            silent=silent,
            store_history=store_history,
            allow_stdin=False,  # `input()` raises instead of hanging the turn forever
            stop_on_error=True,
        )

        stdout: list[str] = []
        stderr: list[str] = []
        displays: list[str] = []
        result_repr: str | None = None
        error: KernelError | None = None
        execution_count: int | None = None
        reply_status: str | None = None
        idle = False
        timed_out = False

        deadline = started + timeout
        with self._pump.subscribe(msg_id) as queue:
            while not (idle and reply_status is not None):
                try:
                    channel, message = await asyncio.wait_for(
                        queue.get(), max(deadline - time.monotonic(), 0.0)
                    )
                except asyncio.TimeoutError:
                    if timed_out:
                        break  # interrupt did not land; the kernel is wedged
                    timed_out = True
                    await self.interrupt()
                    deadline = time.monotonic() + INTERRUPT_GRACE
                    continue

                content: dict[str, Any] = message["content"]
                msg_type: str = message["msg_type"]

                if channel == "shell":
                    if msg_type == "execute_reply":
                        reply_status = content.get("status")
                        execution_count = content.get("execution_count", execution_count)
                    continue

                if msg_type == "stream":
                    name = content.get("name", "stdout")
                    text = content.get("text", "")
                    (stdout if name == "stdout" else stderr).append(text)
                    if on_stream is not None:
                        outcome = on_stream(name, text)
                        if inspect.isawaitable(outcome):
                            await outcome
                elif msg_type == "execute_input":
                    execution_count = content.get("execution_count", execution_count)
                elif msg_type == "execute_result":
                    result_repr = content.get("data", {}).get("text/plain")
                    execution_count = content.get("execution_count", execution_count)
                elif msg_type == "display_data":
                    text = content.get("data", {}).get("text/plain")
                    if text:
                        displays.append(text)
                elif msg_type == "error":
                    error = KernelError(
                        ename=content.get("ename", ""),
                        evalue=content.get("evalue", ""),
                        traceback=_strip_ansi("\n".join(content.get("traceback", []))),
                    )
                elif msg_type == "clear_output":
                    stdout.clear()
                    stderr.clear()
                    displays.clear()
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    idle = True

        return ExecResult(
            outcome=self._outcome(timed_out, reply_status, error),
            stdout="".join(stdout),
            stderr="".join(stderr),
            result_repr=result_repr,
            displays=displays,
            error=error,
            execution_count=execution_count,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _outcome(timed_out: bool, reply_status: str | None, error: KernelError | None) -> ExecOutcome:
        if timed_out:
            return ExecOutcome.TIMEOUT
        if error is not None and error.ename == "KeyboardInterrupt":
            return ExecOutcome.INTERRUPTED
        if reply_status is None:
            # The loop only exits without a reply if the channels went away under us.
            return ExecOutcome.DEAD
        if reply_status == "aborted":
            return ExecOutcome.INTERRUPTED
        if reply_status == "error" or error is not None:
            return ExecOutcome.ERROR
        return ExecOutcome.OK
