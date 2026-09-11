"""
Agent — the turn loop. Message in, events out.

    await agent.push("Add rate limiting")      # queue work
    async for ev in agent.events(): ...        # drive it, forever

    async for ev in agent.run("Add rate limiting"): ...   # one turn, to completion

`run()` is `push()` then `events()` until the first terminal event. The split is not
ergonomics: a `push` arriving mid-turn queues for the *next* turn instead of splicing
into the one in flight, which is what lets you attach to a working agent and redirect
it without destroying what it is doing. To stop it, `interrupt()`.

**The stop condition is that the model stopped calling tools.** No `answer` dict, no
`ready` flag — models already end a turn by talking, and a protocol on top of that is
a protocol the model has to be taught.

This imports session, harness and providers; none of them import back. `task` lives
here for that reason — a tool that spawns an `Agent` would point the harness at its
own caller.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncGenerator, AsyncIterator, Sequence

from stcode.core.agent.events import (
    PREVIEW_CHARS,
    AgentEvent,
    AgentFailed,
    SupervisorNudge,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from stcode.core.agent.supervisor import Supervisor
from stcode.core.harness import Harness
from stcode.core.harness.approvals import DEFAULT_APPROVAL_MODE, ApprovalMode
from stcode.core.providers.gateway import Difficulty, LLMGateway
from stcode.core.providers.types import (
    MessageStop,
    ReasoningDelta,
    TextDelta,
    ToolCallEnd,
    Usage,
)
from stcode.core.session import Session

DEFAULT_MAX_TURNS = 40
"""Tool-call ceiling within one turn — the only thing between a model that keeps
grepping and an unbounded bill. Hitting it is reported, not swallowed."""


class Agent:
    """One agent: a gateway, a harness, a session, and the loop that joins them."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        harness: Harness,
        session: Session,
        difficulty: Difficulty = "high",
        max_turns: int = DEFAULT_MAX_TURNS,
        max_concurrent: int = 4,
        owns_gateway: bool = False,
        supervisor: Supervisor | None = None,
        mailbox: Any = None,
    ) -> None:
        self.gateway = gateway
        self.harness = harness
        self.session = session
        self.difficulty = difficulty
        self.max_turns = max_turns
        self.supervisor = supervisor
        """Checked every `supervisor.every` iterations *inside* a turn. None for
        sub-agents: a nudge arrives about when a one-shot worker is finishing anyway."""

        self.mailbox = mailbox
        """This role's Mailbox, in team mode. Drained at the top of every turn. Typed
        loosely so `core/agent` need not import `core/team` just to hold a reference."""
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._owns_gateway = owns_gateway
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._interrupted = asyncio.Event()
        self._busy = False

    # ---- construction ----

    @classmethod
    async def create(
        cls,
        config: Any,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        approval_mode: ApprovalMode | None = None,
        session: Session | None = None,
        gateway: LLMGateway | None = None,
        **harness_kwargs: Any,
    ) -> "Agent":
        """Build an agent from a loaded `GatewayConfig`.

        `config` is typed loosely so the agent depends on the values it reads, not on
        the on-disk file format.
        """
        owns_gateway = gateway is None
        gateway = gateway or LLMGateway(
            providers=config.providers, routing=config.routing, retry=config.retry
        )
        # `role` names the prompt section and the session `meta` record. It does not by
        # itself turn on team mode — `[team] role` is a container declaring that a shared
        # volume is mounted, and a solo session naming a role should not go looking.
        team = getattr(config, "team", None)
        role = role or (team.role if team else "")
        joins_team = bool(team and team.role)

        # `setdefault`, not a keyword: a caller passing `load_mcp=False` meant it, and
        # passing both would be a duplicate-argument TypeError.
        harness_kwargs.setdefault("load_mcp", config.mcp.enabled)
        harness_kwargs.setdefault("mcp_expose", config.mcp.expose)
        harness_kwargs.setdefault("role", role)
        harness = await Harness.create(
            cwd=cwd,
            approval_mode=approval_mode or config.defaults.approval_mode,
            **harness_kwargs,
        )
        session = session or Session.create(
            cwd=harness.context.cwd,
            role=role,
            model=config.defaults.model,
            directory=config.session.dir,
        )
        harness.session_id = session.id

        agent = cls(
            gateway=gateway,
            harness=harness,
            session=session,
            difficulty=config.agent.difficulty,
            max_turns=config.agent.max_turns,
            max_concurrent=config.agent.max_concurrent,
            owns_gateway=owns_gateway,
            supervisor=(
                Supervisor(
                    gateway,
                    every=config.supervisor.every,
                    window=config.supervisor.window,
                    difficulty=config.supervisor.difficulty,
                )
                if config.supervisor.enabled and harness.depth == 0
                else None
            ),
        )
        if config.agent.enable_task and harness.depth < config.agent.max_depth:
            agent.enable_task()
        if joins_team and team is not None:
            agent.enable_team(team.shared_dir, role)
        return agent

    def enable_task(self) -> None:
        """Register the sub-agent tool on this agent's harness.

        Registered rather than built in: `task` needs *this* agent to spawn from, and
        `core/harness/` may not know `core/agent/` exists.
        """
        from stcode.core.agent.task import make_task_tool

        self.harness.registry.register(make_task_tool(self), replace=True)
        self.harness.allow("task")

    def enable_team(self, shared_dir: str, role: str) -> None:
        """Join a team: a mailbox on the shared volume, and `send_message`.

        Registered from here for the same reason as `task` — the tool closes over this
        agent's mailbox, and `core/harness` must not import `core/team`.

        `task` is *removed*: in team mode the parallelism is containers, so a sub-agent
        inside a role container re-answers a question the architecture already answered.
        """
        from stcode.core.team import Mailbox, make_send_message_tool

        mailbox = Mailbox(shared_dir, role)
        try:
            mailbox.ensure()
        except OSError as exc:
            raise RuntimeError(
                f"Team mode needs a writable shared volume at {mailbox.root} ({exc}). "
                "Mount one, point [team] shared_dir somewhere else, or clear [team] role "
                "to run solo."
            ) from exc
        self.mailbox = mailbox
        self.harness.registry.register(make_send_message_tool(self.mailbox), replace=True)
        self.harness.allow("send_message")
        self.harness.teammates = [name for name in self.mailbox.roles() if name != role]
        if self.harness.allowed and "task" in self.harness.allowed:
            self.harness.allowed.remove("task")

    # ---- driving ----

    async def push(self, text: str) -> None:
        """Queue a message. Never interrupts the turn in flight."""
        await self._inbox.put(text)

    @property
    def busy(self) -> bool:
        return self._busy

    async def events(self) -> AsyncGenerator[AgentEvent, None]:
        """Run queued messages forever, yielding what happens.

        **One consumer.** Whoever iterates this drives the loop, so there is no
        background task to leak and no second loop to keep in sync. Two iterators would
        race for the same queue — fan out from one instead.
        """
        while True:
            text = await self._inbox.get()
            async with aclosing(self._run_turn(text)) as turn:
                async for event in turn:
                    yield event

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        """One turn, start to finish. The shortcut, not a second loop.

        `aclosing` because returning early from an `async for` leaves the generator
        below suspended until the GC gets to it — possibly after the event loop has
        closed, stranding any network connection down there.

        One level is not enough: `GeneratorExit` into `events()` unwinds its own frame
        but never awaits `_run_turn().aclose()`. So every level wraps the one under it
        — `run` → `events` → `_run_turn` → `_stream` → the provider's.
        """
        await self.push(text)
        async with aclosing(self.events()) as stream:
            async for event in stream:
                yield event
                if isinstance(event, (TurnFinished, AgentFailed)):
                    return

    async def result(self, text: str) -> str:
        """Run one turn and return just the final text. What `task` hands its parent."""
        async for event in self.run(text):
            if isinstance(event, TurnFinished):
                return event.text
            if isinstance(event, AgentFailed):
                return f"[the sub-agent did not finish] {event.message}"
        return ""

    async def interrupt(self) -> None:
        """Stop the turn in flight at its next checkpoint.

        Sets both the flag the loop checks between steps and the cancellation event
        every `Runtime` watches. Tools in flight see it at their next
        `raise_if_cancelled()`; the stream stops at its next event.
        """
        self._interrupted.set()
        self.harness.cancel.set()

    # ---- the loop ----

    async def _run_turn(self, text: str) -> AsyncGenerator[AgentEvent, None]:
        self._interrupted.clear()
        self.harness.cancel.clear()
        self._busy = True
        try:
            self._drain_inbox()
            self.session.append(type="user", content=text)

            for iteration in range(self.max_turns):
                reply, calls, usage, failure = "", [], Usage(), None
                try:
                    async with aclosing(self._stream()) as stream:
                        async for event in stream:
                            if isinstance(event, TextDelta):
                                reply += event.text
                                yield event
                            elif isinstance(event, ReasoningDelta):
                                yield event
                            elif isinstance(event, ToolCallEnd):
                                calls.append(event)
                            elif isinstance(event, MessageStop):
                                usage = event.usage
                            if self._interrupted.is_set():
                                break
                except Exception as exc:  # noqa: BLE001 — any provider failure ends the turn
                    failure = f"{type(exc).__name__}: {exc}"

                # Every call needs a result before the next request, so nothing between
                # here and the tool loop may return early: a `tool_use` block with no
                # matching `tool_result` is a 400 on the following turn.
                if failure is not None:
                    self.session.append(type="error", message=failure)
                    yield AgentFailed(message=failure)
                    return

                if reply or calls:
                    self.session.append(type="assistant", content=reply)
                for call in calls:
                    self.session.append(
                        type="tool_call", id=call.id, name=call.name, arguments=call.input
                    )

                if self._interrupted.is_set() and not calls:
                    yield AgentFailed(message="Interrupted.")
                    return
                if not calls:
                    yield TurnFinished(text=reply, usage=usage)
                    return

                async for event in self._run_tools(calls):
                    yield event

                if self._interrupted.is_set():
                    yield AgentFailed(message="Interrupted.")
                    return

                async for event in self._supervise(iteration + 1):
                    yield event

            yield AgentFailed(
                message=(
                    f"Reached the ceiling of {self.max_turns} tool calls in one turn "
                    "without finishing. The trajectory is in the session log."
                )
            )
        finally:
            self._busy = False

    def _drain_inbox(self) -> None:
        """Take delivery of anything waiting, at the top of the turn.

        Written as `inbox` records, which `Session.messages()` renders as prefixed user
        messages — the route supervisor nudges take too, for the same reason: the
        cached system prefix must not move.

        Top of the turn, never mid-turn. A message arriving while the agent works waits,
        exactly as a `push` does. Splicing into a turn destroys work in progress.
        """
        if self.mailbox is None:
            return
        for message in self.mailbox.drain():
            self.session.append(
                type="inbox",
                **{"from": message.sender},
                subject=message.subject,
                body=message.body,
                refs=message.refs,
            )

    async def _supervise(self, iteration: int) -> AsyncGenerator[AgentEvent, None]:
        """Let the supervisor look, on its own schedule.

        The nudge goes in as a `supervisor` record, rendered as a prefixed `user`
        message. Never into the system prompt — that is the cached prefix, and moving
        it would make every piece of advice cost a full cache miss.
        """
        if self.supervisor is None or not self.supervisor.due(iteration):
            return
        nudge = await self.supervisor.check(self.session.records())
        if not nudge:
            return
        self.session.append(type="supervisor", content=nudge)
        yield SupervisorNudge(text=nudge)

    async def _stream(self) -> AsyncIterator[Any]:
        async for event in self.gateway.stream(
            self.session.messages(),
            system=self.harness.system_prompt(),
            tools=self.harness.tool_definitions(),
            difficulty=self.difficulty,
        ):
            if isinstance(event, MessageStop):
                # The gateway emits usage, the agent records it. A gateway importing
                # Session would depend on its own caller.
                self.session.append(
                    type="usage",
                    difficulty=self.difficulty,
                    stop_reason=event.stop_reason,
                    **event.usage.model_dump(),
                )
            yield event

    async def _run_tools(self, calls: Sequence[ToolCallEnd]) -> AsyncGenerator[AgentEvent, None]:
        """Run this turn's calls concurrently and record every result.

        No `return_exceptions`: `Harness.invoke()` never raises for a tool-level
        failure, so an exception here is a harness bug, not news for the model.
        """
        for call in calls:
            yield ToolStarted(id=call.id, name=call.name, arguments=call.input)

        async def invoke(call: ToolCallEnd) -> Any:
            async with self._semaphore:
                return await self.harness.invoke(call.name, call.input, tool_call_id=call.id)

        results = await asyncio.gather(*(invoke(call) for call in calls))

        for call, result in zip(calls, results):
            self.session.append(
                type="tool_result",
                id=call.id,
                name=call.name,
                content=result.content,
                is_error=result.is_error,
            )
            yield ToolFinished(
                id=call.id,
                name=call.name,
                ok=not result.is_error,
                preview=result.content[:PREVIEW_CHARS],
            )

    # ---- lifecycle ----

    async def aclose(self) -> None:
        """Release the harness (MCP, background shells) and close the transcript.

        The gateway closes only if this agent built it — a sub-agent shares its
        parent's, and closing that mid-turn would kill the parent's stream.
        """
        await self.harness.aclose()
        self.session.close()
        if self._owns_gateway:
            await self.gateway.aclose()

    async def __aenter__(self) -> "Agent":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"Agent({self.harness.agent_name}, session={self.session.id})"


__all__ = ["DEFAULT_APPROVAL_MODE", "DEFAULT_MAX_TURNS", "Agent"]
