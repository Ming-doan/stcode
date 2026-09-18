"""
SessionRunner — one live agent, zero or more watchers.

Where the daemon's three hard points actually live:

**Detach does not kill the agent.** The runner, not the connection, is the single
consumer of `agent.events()`. Clients are sinks it fans out to; losing the last one
changes nothing about the turn in flight, and events keep landing in the session for a
re-attaching client to replay.

**Approval is request–response.** `on_approval` is `async (ApprovalRequest) -> bool`,
which over a socket becomes: broadcast `approval_request`, park a `Future` under the
`execution_id`, resolve it when a client answers. The harness needed no change —
`ApprovalRequest` already carried the id. Same mechanism for `ask_user_question`, with
the id minted here because `Question` has no field for one.

**A push mid-turn queues.** That is `Agent.push()`'s promise; the runner just hands the
text over.

The failure mode worth naming: a turn blocked on approval whose last client drops would
wait forever on a `Future` nobody can resolve. So losing the last watcher fails every
pending request with `ToolDenied`, and the model reads why and moves on.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import aclosing
from typing import Any

from stcode.core.agent import Agent
from stcode.core.daemon.protocol import (
    ApprovalRequested,
    ErrorMessage,
    Progress,
    QuestionAsked,
    SessionOpened,
    event_frame,
)
from stcode.core.harness.approvals import ApprovalMode
from stcode.core.harness.errors import ToolDenied
from stcode.core.harness.tools.base import ApprovalRequest, Question

NO_CLIENT = (
    "No client is attached to this session, so there is nobody to {verb}. Continue with "
    "what you can decide yourself and state the assumption you made."
)

WAKE = (
    "A message arrived while you were idle. It is above this line. Deal with it, or say "
    "why it is not yours to deal with."
)
"""What an inbox-woken turn is pushed. Short on purpose — the message is already in
the history as an `inbox` record, and repeating it pays for it twice."""


class SessionRunner:
    """One session the daemon is holding: its agent, its watchers, its open requests."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.id = agent.session.id
        self._sinks: set["asyncio.Queue[dict[str, Any]]"] = set()
        # execution_id -> (future the tool is parked on, the frame that asked). The
        # frame is kept so a client attaching mid-question sees what is being waited
        # for, rather than a session that looks hung.
        self._pending: dict[str, tuple["asyncio.Future[Any]", dict[str, Any]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None

        # The agent asks; this runner answers over the wire. Wired here rather than in
        # `Harness.create`, because the harness must not know a socket exists.
        agent.attach(
            on_approval=self.request_approval, on_ask=self.ask, on_progress=self.progress
        )

    # ---- lifecycle ----

    def start(self) -> None:
        """Begin draining `agent.events()`. Idempotent — attach may call it again."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump(), name=f"stcode-session-{self.id}")

    async def _pump(self) -> None:
        """The single consumer of the agent's event stream.

        `events()` is one-consumer — two iterators would race for the same inbox — so
        the fan-out happens on this side of it.
        """
        try:
            async with aclosing(self.agent.events()) as stream:
                async for event in stream:
                    self.broadcast(event_frame(self.id, event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead pump must be visible, not silent
            self.broadcast(
                ErrorMessage(session=self.id, message=f"{type(exc).__name__}: {exc}").model_dump()
            )

    def watch_inbox(self, mailbox: Any, interval: float = 1.0) -> None:
        """Start a turn when a message lands and the agent is idle.

        Polling, not inotify: one `listdir` a second costs nothing measurable, behaves
        the same on every filesystem a volume might be, and needs no dependency.

        Only when **idle**. A message arriving mid-turn is already delivered — the agent
        drains its inbox at the top of the next turn anyway — and interrupting would
        break the rule that a push never splices into a turn in flight.
        """
        if self._watch is not None and not self._watch.done():
            return
        self._watch = asyncio.create_task(
            self._poll_inbox(mailbox, interval), name=f"stcode-inbox-{self.id}"
        )

    async def _poll_inbox(self, mailbox: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                if self.agent.busy or not mailbox.pending():
                    continue
                await self.push(WAKE)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a broken volume must not be silent
                self.broadcast(
                    ErrorMessage(session=self.id, message=f"inbox watch: {exc}").model_dump()
                )
                return

    async def aclose(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
            self._watch = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._fail_pending("the session is shutting down")
        await self.agent.aclose()

    # ---- watchers ----

    def subscribe(self, sink: "asyncio.Queue[dict[str, Any]]") -> None:
        self._sinks.add(sink)

    def unsubscribe(self, sink: "asyncio.Queue[dict[str, Any]]") -> None:
        self._sinks.discard(sink)
        if not self._sinks:
            self._fail_pending("the last client detached")

    @property
    def watchers(self) -> int:
        return len(self._sinks)

    def broadcast(self, frame: dict[str, Any]) -> None:
        """Fan one frame out to every watcher.

        Unbounded queues and `put_nowait`: broadcasting must never block the agent loop
        on a slow client, and dropping a `tool_finished` to save memory would leave that
        client's transcript permanently wrong.
        """
        for sink in list(self._sinks):
            sink.put_nowait(frame)

    def describe(self) -> SessionOpened:
        meta = self.agent.session.meta()
        return SessionOpened(
            id=self.id,
            cwd=str(meta.get("cwd", "")),
            role=str(meta.get("role", "")),
            model=str(meta.get("model", "")),
            approval_mode=self.agent.harness.approval_mode,
            busy=self.agent.busy,
        )

    def open_requests(self) -> list[dict[str, Any]]:
        """Requests the agent is currently parked on, for a client that just attached."""
        return [frame for _future, frame in self._pending.values()]

    # ---- driving ----

    async def push(self, text: str) -> None:
        self.start()
        await self.agent.push(text)

    async def interrupt(self) -> None:
        await self.agent.interrupt()

    def set_mode(self, mode: ApprovalMode) -> None:
        """Change the approval mode of the live agent.

        The harness holds the mode and `tool_definitions()` is read fresh each request,
        so this takes effect on the next model call. It does not reach a tool already in
        flight — that call was gated under the mode in force when it started.
        """
        self.agent.harness.approval_mode = mode

    # ---- request/response over the wire ----

    async def request_approval(self, request: ApprovalRequest) -> bool:
        answer = await self._await_client(
            request.execution_id,
            ApprovalRequested.of(self.id, request),
            verb=f"approve `{request.tool_name}`",
        )
        return bool(answer)

    async def ask(self, question: Question) -> str:
        execution_id = uuid.uuid4().hex[:12]
        answer = await self._await_client(
            execution_id,
            QuestionAsked.of(self.id, execution_id, question),
            verb="answer a question",
        )
        return str(answer)

    async def progress(self, text: str) -> None:
        self.broadcast(Progress(session=self.id, text=text).model_dump())

    async def _await_client(self, execution_id: str, message: Any, *, verb: str) -> Any:
        """Ask the attached clients something and block this tool until one answers.

        `ToolDenied` for "nobody is attached" rather than False: both end the call, but
        `Tool.invoke`'s generic denial text says *the user declined*, and a headless run
        told a human refused it will act on that lie.
        """
        if not self._sinks:
            raise ToolDenied(NO_CLIENT.format(verb=verb))

        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        frame = message.model_dump(mode="json")
        self._pending[execution_id] = (future, frame)
        self.broadcast(frame)
        try:
            return await future
        finally:
            self._pending.pop(execution_id, None)

    def resolve(self, execution_id: str, value: Any) -> bool:
        """Answer one open request. False when the id matches nothing still waiting: a
        late answer to an abandoned request is not an error."""
        entry = self._pending.get(execution_id)
        if entry is None or entry[0].done():
            return False
        entry[0].set_result(value)
        return True

    def _fail_pending(self, reason: str) -> None:
        for future, _frame in list(self._pending.values()):
            if not future.done():
                future.set_exception(ToolDenied(f"The request was abandoned: {reason}."))
        self._pending.clear()

    def __repr__(self) -> str:
        return f"SessionRunner({self.id}, watchers={self.watchers}, busy={self.agent.busy})"


__all__ = ["NO_CLIENT", "SessionRunner"]
