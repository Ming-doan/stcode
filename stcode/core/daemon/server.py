"""
Daemon — the first layer, not the last.

One process, **many sessions**, addressed by id. Solo mode runs one daemon for every
project you work on — one session per repo, not one process per repo. A container
usually holds one, but nothing forbids more.

    daemon = Daemon(config)
    await daemon.start()          # bind, then get on with your life
    await daemon.serve_forever()  # or just block here

    async with Daemon(config) as daemon: ...   # start + close

What it owns, and deliberately nothing else: a socket, the session registry, the
connections, and one shared `LLMGateway`. The gateway is shared because it caches
provider clients by `(provider, key, base_url)` and a daemon holding eight sessions
should hold one connection pool, not eight. `Agent.create` is handed it, so no agent
closes it — the daemon does, once, at shutdown.

Approvals and questions do not appear here at all. They are the runner's, because they
belong to a session rather than to a connection: the client that asked may be gone by
the time the answer arrives, and a second client attached to the same session can
answer instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket as socketlib
from pathlib import Path
from types import TracebackType
from typing import Any

from stcode.core.agent import Agent
from stcode.core.configs import GatewayConfig
from stcode.core.daemon.autonomy import AutonomyRefused, guard_autonomy
from stcode.core.daemon.protocol import (
    Answer,
    Approval,
    Attach,
    ClientMessage,
    Create,
    Detach,
    ErrorMessage,
    History,
    Interrupt,
    ProtocolError,
    Push,
    Sessions,
    SessionList,
    SetMode,
    decode,
    encode,
    parse_client_message,
)
from stcode.core.daemon.runner import SessionRunner
from stcode.core.harness.approvals import ApprovalMode
from stcode.core.providers.gateway import LLMGateway
from stcode.core.session import Session

log = logging.getLogger("stcode.daemon")

SHUTDOWN_TIMEOUT = 5.0
"""How long shutdown waits for the listening socket to drain. Bounded because a
shutdown that can hang is a shutdown you learn to `kill -9` instead of trusting."""


class Daemon:
    """A socket, a session registry, and the connections watching them."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        has_approver: bool = False,
        gateway: LLMGateway | None = None,
    ) -> None:
        """`has_approver` is False for everything stcode ships — see `autonomy.py` for
        why an attached human is not one under `full-auto`. `gateway` is for tests and
        for embedding; left None, the daemon builds and owns one."""
        self.config = config
        self.has_approver = has_approver
        self.sessions: dict[str, SessionRunner] = {}
        self._connections: set["_Connection"] = set()
        self._gateway = gateway
        self._owns_gateway = gateway is None
        self._server: asyncio.base_events.Server | None = None

    # ---- lifecycle ----

    @property
    def address(self) -> str:
        """Where clients should look. Also what `stcode daemon` prints."""
        settings = self.config.daemon
        if settings.transport == "unix":
            return str(socket_path(settings.socket))
        return f"{settings.host}:{settings.port}"

    async def start(self) -> None:
        """Bind and begin accepting. Returns as soon as the socket is listening.

        The guard runs *before* the bind: refusing after clients can connect would mean
        the refusal is a message rather than a refusal.
        """
        guard_autonomy(self.config.defaults.approval_mode, self.has_approver)

        settings = self.config.daemon
        if settings.transport == "unix":
            path = socket_path(settings.socket)
            path.parent.mkdir(parents=True, exist_ok=True)
            _clear_stale_socket(path)
            self._server = await asyncio.start_unix_server(self._handle, path=str(path))
            # The socket is the access control on a unix transport: anyone who can open
            # it can drive an agent with your privileges.
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        else:
            self._server = await asyncio.start_server(self._handle, settings.host, settings.port)
        log.info("listening on %s", self.address)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def aclose(self) -> None:
        """Close the door, then everything behind it.

        Order is load-bearing, and not the obvious one. `Server.wait_closed()` waits for
        every *handler* to finish, not just for the listening socket — so closing the
        server while a client is still attached waits for a connection that is waiting
        for us, forever. Clients first, then the server, then the sessions (each of
        which kills its background shells and MCP connections), then the gateway.
        """
        for connection in list(self._connections):
            await connection.aclose()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), SHUTDOWN_TIMEOUT)
            self._server = None
        for runner in list(self.sessions.values()):
            await runner.aclose()
        self.sessions.clear()
        if self._gateway is not None and self._owns_gateway:
            await self._gateway.aclose()
            self._gateway = None
        if self.config.daemon.transport == "unix":
            with contextlib.suppress(OSError):
                socket_path(self.config.daemon.socket).unlink()

    async def __aenter__(self) -> "Daemon":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---- sessions ----

    def gateway(self) -> LLMGateway:
        if self._gateway is None:
            self._gateway = LLMGateway(
                providers=self.config.providers,
                routing=self.config.routing,
                retry=self.config.retry,
            )
        return self._gateway

    async def create_session(
        self,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        approval_mode: ApprovalMode | None = None,
        session: Session | None = None,
    ) -> SessionRunner:
        """Build an agent and register it. The guard applies per session, not just per
        daemon: a `full-auto` session on a `suggest` daemon is the same hole."""
        mode = approval_mode or self.config.defaults.approval_mode
        guard_autonomy(mode, self.has_approver)

        agent = await Agent.create(
            self.config,
            cwd=cwd,
            role=role,
            approval_mode=mode,
            session=session,
            gateway=self.gateway(),
        )
        runner = SessionRunner(agent)
        runner.start()
        self.sessions[runner.id] = runner
        log.info("session %s created at %s", runner.id, agent.harness.context.cwd)
        return runner

    async def resume_session(self, session_id: str) -> SessionRunner:
        """Attach to a session this daemon is not holding by reading it off disk.

        `Session(path)` adopts the records already in the file, so the resumed agent's
        `messages()` is the conversation that actually happened rather than an empty one
        appended to an old transcript.
        """
        existing = self.sessions.get(session_id)
        if existing is not None:
            return existing
        session = Session.resume(session_id, directory=self.config.session.dir)
        meta = session.meta()
        return await self.create_session(
            cwd=meta.get("cwd") or None, role=str(meta.get("role", "")), session=session
        )

    def session_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        """What this daemon is holding, merged over what is on disk.

        Live sessions are the same rows with `live` and `busy` set, so a client can tell
        "running right now" from "a transcript you could resume" without a second call.
        """
        rows = Session.list(limit, directory=self.config.session.dir)
        by_id = {str(row.get("id", "")): row for row in rows}
        for runner in self.sessions.values():
            row = by_id.setdefault(runner.id, {"id": runner.id})
            row.update(runner.describe().model_dump(mode="json"))
            row["type"] = "meta"
        for row in by_id.values():
            row.setdefault("live", False)
            row.setdefault("busy", False)
        live = [row for row in by_id.values() if row["id"] in self.sessions]
        for row in live:
            row["live"] = True
        return sorted(by_id.values(), key=lambda row: str(row.get("id", "")), reverse=True)[:limit]

    # ---- connections ----

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = _Connection(self, reader, writer)
        self._connections.add(connection)
        try:
            await connection.run()
        finally:
            self._connections.discard(connection)
            await connection.aclose()


class _Connection:
    """One client. Reads lines, writes frames, and remembers which session it is on.

    Two tasks, deliberately: the read loop dispatches, and a writer task drains one
    outbound queue. The runner broadcasts *into that queue* rather than to a socket, so
    a client that has stopped reading slows nothing down but itself.
    """

    def __init__(
        self, daemon: Daemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._daemon = daemon
        self._reader = reader
        self._writer = writer
        self._outbox: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._drain_task = asyncio.create_task(self._drain(), name="stcode-conn-writer")
        self._attached: set[str] = set()
        self._current: str = ""

    # ---- io ----

    async def _drain(self) -> None:
        while True:
            frame = await self._outbox.get()
            try:
                self._writer.write(encode(frame))
                await self._writer.drain()
            except (ConnectionError, OSError):
                return  # The client is gone; the read loop is about to notice too.

    def send(self, message: Any) -> None:
        self._outbox.put_nowait(
            message if isinstance(message, dict) else message.model_dump(mode="json")
        )

    async def run(self) -> None:
        # StreamReader iterates by line, which is exactly the framing — no buffer of
        # our own to get wrong. A line longer than the limit raises, and that is right:
        # a client sending an unterminated megabyte is not a client we can talk to.
        async for line in self._reader:
            if not line.strip():
                continue
            try:
                message = parse_client_message(decode(line))
            except ProtocolError as exc:
                self.send(ErrorMessage(message=str(exc)))
                continue
            try:
                await self._dispatch(message)
            except Exception as exc:  # noqa: BLE001 — one bad request must not drop the client
                log.exception("dispatch failed")
                self.send(ErrorMessage(message=f"{type(exc).__name__}: {exc}"))

    async def aclose(self) -> None:
        """Detach from everything, then close the socket. **Never touches the agents** —
        that is the whole promise of the daemon (§8 point 2)."""
        for session_id in list(self._attached):
            self._detach(session_id)
        self._drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._drain_task
        with contextlib.suppress(ConnectionError, OSError):
            self._writer.close()
            await self._writer.wait_closed()

    # ---- dispatch ----

    async def _dispatch(self, message: ClientMessage) -> None:
        match message:
            case Create():
                runner = await self._daemon.create_session(
                    cwd=message.cwd or None,
                    role=message.role,
                    approval_mode=message.approval_mode,
                )
                self._attach(runner, replay=False)
            case Attach():
                runner = await self._daemon.resume_session(message.session)
                self._attach(runner, replay=message.replay)
            case Detach():
                self._detach(self._target_id(message.session))
            case Sessions():
                self.send(SessionList(sessions=self._daemon.session_summaries(message.limit)))
            case Push():
                await self._runner(message.session).push(message.text)
            case Interrupt():
                await self._runner(message.session).interrupt()
            case Approval():
                self._runner(message.session).resolve(message.execution_id, message.approved)
            case Answer():
                self._runner(message.session).resolve(message.execution_id, message.text)
            case SetMode():
                runner = self._runner(message.session)
                # `create` is not the only door a mode comes through, so the guard
                # cannot live only there: cycling to `full-auto` mid-session would
                # otherwise be the override flag rule 5 says does not exist.
                try:
                    guard_autonomy(message.mode, self._daemon.has_approver)
                except AutonomyRefused as refusal:
                    self.send(ErrorMessage(session=runner.id, message=str(refusal)))
                else:
                    runner.set_mode(message.mode)
                # Either way, say what the mode now *is* — the client asked for a change
                # and must not be left showing one that was refused.
                self.send(runner.describe())

    def _attach(self, runner: SessionRunner, *, replay: bool) -> None:
        """Subscribe, then replay, then join the live stream.

        The order is what makes re-attach lossless: subscribing first means events
        arriving during the replay queue up behind it rather than falling in the gap
        between reading the file and joining the stream. A record may therefore appear
        twice; a duplicate is recoverable, a hole is not.
        """
        runner.subscribe(self._outbox)
        self._attached.add(runner.id)
        self._current = runner.id
        self.send(runner.describe())
        if replay:
            self.send(History(session=runner.id, records=runner.agent.session.records()))
        # Anything the agent is parked on, so a client attaching mid-turn can answer it
        # instead of watching a session that looks hung.
        for frame in runner.open_requests():
            self.send(frame)
        runner.start()

    def _detach(self, session_id: str) -> None:
        runner = self._daemon.sessions.get(session_id)
        if runner is not None:
            runner.unsubscribe(self._outbox)
        self._attached.discard(session_id)
        if self._current == session_id:
            self._current = next(iter(self._attached), "")

    def _target_id(self, session_id: str) -> str:
        return session_id or self._current

    def _runner(self, session_id: str) -> SessionRunner:
        target = self._target_id(session_id)
        if not target:
            raise ProtocolError("no session — send `create` or `attach` first")
        runner = self._daemon.sessions.get(target)
        if runner is None:
            raise ProtocolError(f"this daemon is not holding session {target!r}")
        return runner


# ---- unix socket housekeeping ---------------------------------------------------


def socket_path(value: str) -> Path:
    return Path(value).expanduser()


def _clear_stale_socket(path: Path) -> None:
    """Remove a socket file left behind by a daemon that died.

    A crashed daemon leaves the path in place and `bind` then fails with "address
    already in use", which is indistinguishable from a healthy daemon already running.
    Connecting is the only way to tell them apart: a refused connection means nobody is
    home, so the file is debris.
    """
    if not path.exists():
        return
    probe = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError, socketlib.timeout, OSError):
        with contextlib.suppress(OSError):
            path.unlink()
        return
    finally:
        probe.close()
    raise OSError(f"a daemon is already listening on {path}")


__all__ = ["Daemon", "socket_path"]
