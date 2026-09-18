"""
Daemon — one process, **many sessions**, addressed by id.

    daemon = Daemon(config)
    await daemon.start()          # bind, then get on with your life
    await daemon.serve_forever()  # or just block here

    async with Daemon(config) as daemon: ...   # start + close

Solo mode runs one daemon for every project — one session per repo, not one process per
repo. A container usually holds one, but nothing forbids more.

It owns a socket, the session registry, the connections, and one shared `LLMGateway`.
Shared because the gateway caches provider clients by `(provider, key, base_url)`, and
a daemon holding eight sessions should hold one connection pool. No agent closes it —
the daemon does, once, at shutdown.

Approvals and questions are not here at all: they belong to a session, not a
connection. The client that asked may be gone by the time the answer arrives, and a
second client on the same session can answer instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket as socketlib
from pathlib import Path
from types import TracebackType
from typing import Any

from stcode import __version__
from stcode.core.agent import Agent
from stcode.core.common import trace
from stcode.core.common.paths import default_config_path
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
    Info,
    Interrupt,
    ProtocolError,
    Push,
    Sessions,
    SessionList,
    SetMeta,
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
"""How long shutdown waits for the listening socket to drain. Bounded, because a
shutdown that can hang is one you learn to `kill -9` instead of trusting."""


class Daemon:
    """A socket, a session registry, and the connections watching them."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        has_approver: bool = False,
        gateway: LLMGateway | None = None,
    ) -> None:
        """`has_approver` is False for everything stcode ships — `autonomy.py` says why
        an attached human is not one under `full-auto`. `gateway` is for tests and for
        embedding; left None, the daemon builds and owns one."""
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
        """Bind and begin accepting. Returns once the socket is listening.

        The guard runs *before* the bind: refusing after clients can connect makes the
        refusal a message rather than a refusal.
        """
        guard_autonomy(self.config.defaults.approval_mode, self.has_approver)

        settings = self.config.daemon
        if settings.transport == "unix":
            path = socket_path(settings.socket)
            path.parent.mkdir(parents=True, exist_ok=True)
            _clear_stale_socket(path)
            self._server = await asyncio.start_unix_server(self._handle, path=str(path))
            # The socket is the access control here: anyone who can open it can drive
            # an agent with your privileges.
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

        Order is load-bearing and not the obvious one: `Server.wait_closed()` waits for
        every *handler*, not just the listening socket, so closing the server with a
        client still attached waits forever on a connection that is waiting for us.
        Clients, then the server, then the sessions, then the gateway.
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
        # Last, and only here: spans are batched, so without a flush the final turn of
        # a short run — the one you were watching — dies in a buffer. A no-op when
        # tracing was never switched on.
        trace.shutdown()

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
        """Build an agent and register it. The guard applies per session as well as per
        daemon — a `full-auto` session on a `suggest` daemon is the same hole."""
        mode = approval_mode or self.config.defaults.approval_mode
        guard_autonomy(mode, self.has_approver)

        agent = await Agent.create(
            self.config,
            cwd=cwd,
            role=role or self.config.team.role,
            approval_mode=mode,
            session=session,
            gateway=self.gateway(),
        )
        runner = SessionRunner(agent)
        runner.start()
        # Team mode: a role container reacts to a colleague rather than waiting to be
        # poked by a human who may not be attached.
        if agent.mailbox is not None and self.config.team.wake_on_message:
            runner.watch_inbox(agent.mailbox, self.config.team.poll_interval)
        self.sessions[runner.id] = runner
        log.info("session %s created at %s", runner.id, agent.harness.context.cwd)
        return runner

    async def resume_session(self, session_id: str) -> SessionRunner:
        """Attach to a session this daemon is not holding, by reading it off disk.

        `Session(path)` adopts the records already there, so the resumed agent's
        `messages()` is the conversation that happened rather than an empty one
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

    async def drop_unused(self, runner: SessionRunner) -> None:
        """Forget a session that never wrote anything and has nobody watching.

        `/clear` is a `create`, so without this the daemon holds every session anybody
        opened and walked away from, for as long as it runs.

        A session that **has** written a record is never dropped, however long its last
        client has been gone: detach does not kill the agent, and that rule has no
        exceptions. The test is the transcript, not the clock.
        """
        if runner.watchers or runner.agent.session.started:
            return
        self.sessions.pop(runner.id, None)
        await runner.aclose()
        log.info("session %s dropped — never used", runner.id)

    def session_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        """What this daemon is holding, merged over what is on disk.

        Live sessions are the same rows with `live` and `busy` set, so one call tells a
        client "running right now" apart from "a transcript you could resume".
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
    """One client. Reads lines, writes frames, remembers which session it is on.

    Two tasks: the read loop dispatches, and a writer drains one outbound queue. The
    runner broadcasts into that queue rather than to a socket, so a client that stopped
    reading slows nothing down but itself.
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
        # our own to get wrong. An over-long line raises, correctly: a client sending an
        # unterminated megabyte is not one we can talk to.
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
        that is the whole promise of the daemon."""
        for session_id in list(self._attached):
            await self._detach(session_id)
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
                await self._detach(self._target_id(message.session))
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
            case SetMeta():
                runner = self._runner(message.session)
                runner.set_meta(message.fields())
                # Same discipline as `set_mode`: report what the settings now *are*,
                # so a client never shows a change it only asked for.
                self.send(runner.describe())
            case Info():
                runner = self._runner(message.session)
                self.send(
                    runner.inform(
                        version=__version__,
                        daemon=self._daemon.address,
                        config_path=str(default_config_path()),
                        session_dir=str(self._daemon.config.session.dir),
                    )
                )
            case SetMode():
                runner = self._runner(message.session)
                # `create` is not the only door a mode arrives through, so the guard
                # cannot live only there: cycling to `full-auto` mid-session would
                # otherwise be the override flag that is not supposed to exist.
                try:
                    guard_autonomy(message.mode, self._daemon.has_approver)
                except AutonomyRefused as refusal:
                    self.send(ErrorMessage(session=runner.id, message=str(refusal)))
                else:
                    runner.set_mode(message.mode)
                # Either way, report what the mode now *is*: a client must never be
                # left showing a change that was refused.
                self.send(runner.describe())

    def _attach(self, runner: SessionRunner, *, replay: bool) -> None:
        """Subscribe, then replay, then join the live stream.

        The order is what makes re-attach lossless: subscribing first means events
        arriving during the replay queue behind it instead of falling in the gap. A
        record may appear twice — a duplicate is recoverable, a hole is not.
        """
        runner.subscribe(self._outbox)
        self._attached.add(runner.id)
        self._current = runner.id
        self.send(runner.describe())
        if replay:
            self.send(History(session=runner.id, records=runner.agent.session.records()))
        # Anything the agent is parked on, so a client attaching mid-turn can answer
        # rather than watch a session that looks hung.
        for frame in runner.open_requests():
            self.send(frame)
        runner.start()

    async def _detach(self, session_id: str) -> None:
        runner = self._daemon.sessions.get(session_id)
        if runner is not None:
            runner.unsubscribe(self._outbox)
        self._attached.discard(session_id)
        if self._current == session_id:
            self._current = next(iter(self._attached), "")
        if runner is not None:
            await self._daemon.drop_unused(runner)

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

    A crashed daemon leaves the path in place, so `bind` fails with "address already in
    use" — indistinguishable from a healthy daemon. Connecting is the only way to tell
    them apart: a refused connection means nobody is home, so the file is debris.
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
