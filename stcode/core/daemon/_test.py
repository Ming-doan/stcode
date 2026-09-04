"""
Daemon tests — the protocol, the guard, and the three promises of §8.

The gateway is the only fake, for the same reason it is in `core/agent/_test.py`:
faking the daemon's own pieces would test our beliefs about them. Sessions, harnesses,
sockets and connections are real here, so the end-to-end cases actually open a unix
socket and speak JSONL over it.

What each group is protecting:

* **protocol** — a bad line must produce a diagnosis, not a traceback, and not a
  dropped connection.
* **autonomy** — invariant 5. `full-auto` off the host must be impossible, including
  through the "there is a human watching" argument.
* **runner** — approval is request–response, and the abandoned-request path (last
  client leaves while a tool waits) does not wedge the turn.
* **end to end** — detach does not kill the agent, re-attach replays, a push mid-turn
  queues rather than splicing.
"""

from __future__ import annotations

import asyncio
import functools
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Sequence

import pytest

from stcode.core.configs import GatewayConfig
from stcode.core.daemon import Daemon, DaemonClient, SessionRunner
from stcode.core.daemon.autonomy import AutonomyRefused, guard_autonomy
from stcode.core.daemon.protocol import (
    Approval,
    Create,
    ProtocolError,
    Push,
    decode,
    encode,
    event_frame,
    parse_client_message,
)
from stcode.core.harness.tools.base import ApprovalRequest, Question
from stcode.core.harness.approvals import ToolPermission
from stcode.core.providers.types import (
    Message,
    MessageStop,
    StreamEvent,
    TextDelta,
    ToolCallEnd,
    Usage,
)


# ---- scaffolding ----------------------------------------------------------------
#
# `core/agent/_test.py` drives its coroutines through an explicit loop rather than
# pulling in pytest-asyncio, and this follows it. A fresh loop per test rather than a
# shared one, because these tests bind real sockets and start background tasks: a loop
# that outlived a test would carry the previous daemon's server into the next.


def asynctest(fn: Any) -> Any:
    """Run an `async def` test on its own event loop. Fixtures work unchanged —
    `functools.wraps` keeps the signature pytest introspects."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(fn(*args, **kwargs))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    return wrapper




class ScriptedGateway:
    """Replays one list of events per model call. Same shape as `core/agent/_test.py`."""

    def __init__(self, turns: Sequence[Sequence[StreamEvent]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        # Set to block a turn open, so a test can detach mid-run.
        self.gate: asyncio.Event | None = None

    async def stream(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, **kwargs})
        if self.gate is not None:
            await self.gate.wait()
        if not self._turns:
            raise AssertionError("the agent asked for more turns than the script has")
        for event in self._turns.pop(0):
            yield event

    async def aclose(self) -> None:
        self.closed = True


def _text(*chunks: str) -> list[StreamEvent]:
    return [
        *(TextDelta(text=chunk) for chunk in chunks),
        MessageStop(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=2)),
    ]


def _call(call_id: str, tool: str, **arguments: Any) -> list[StreamEvent]:
    return [
        ToolCallEnd(id=call_id, name=tool, input=arguments),
        MessageStop(stop_reason="tool_use", usage=Usage(input_tokens=20, output_tokens=5)),
    ]


def config_for(tmp_path: Path, **daemon: Any) -> GatewayConfig:
    """A config whose sessions and socket are inside `tmp_path` and nowhere else."""
    config = GatewayConfig()
    config.session.dir = str(tmp_path / "sessions")
    config.daemon.transport = daemon.pop("transport", "unix")
    config.daemon.socket = daemon.pop("socket", str(tmp_path / "d.sock"))
    for key, value in daemon.items():
        setattr(config.daemon, key, value)
    config.defaults.approval_mode = "auto-edit"
    return config


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


async def collect(
    client: DaemonClient, *, until: str, limit: int = 200, timeout: float = 5.0
) -> list[dict[str, Any]]:
    """Read frames until one of type `until` arrives, and return everything read.

    Bounded, and the bound reports what *did* arrive: waiting for an event the daemon
    is never going to send is the failure these tests are most likely to produce, and
    an assertion naming the frames received diagnoses it where a hung run does not.
    """
    frames: list[dict[str, Any]] = []

    async def read() -> None:
        # `aclosing`, because returning out of an `async for` otherwise leaves the
        # generator suspended for the collector to finish whenever it gets round to it
        # — the same discipline `Agent.run` documents.
        async with aclosing(client.events()) as stream:
            async for frame in stream:
                frames.append(frame)
                if frame.get("type") == until or len(frames) >= limit:
                    return

    try:
        await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"no {until!r} within {timeout}s — got {types_of(frames)}"
        ) from None
    return frames


async def until_true(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll for something the daemon does on its own schedule — a dropped connection is
    noticed by a read loop, not announced by the client that dropped."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def types_of(frames: Sequence[dict[str, Any]]) -> list[str]:
    return [str(frame.get("type", "")) for frame in frames]


# ---- protocol -------------------------------------------------------------------


def test_encode_is_one_line_even_with_newlines_inside() -> None:
    line = encode(Push(text="two\nlines"))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert decode(line)["text"] == "two\nlines"


def test_decode_rejects_what_is_not_an_object() -> None:
    with pytest.raises(ProtocolError):
        decode(b"not json")
    with pytest.raises(ProtocolError):
        decode(b"[1, 2]")


def test_parse_names_the_type_the_client_sent() -> None:
    with pytest.raises(ProtocolError, match="push"):
        parse_client_message({"type": "push"})  # no `text`
    with pytest.raises(ProtocolError):
        parse_client_message({"type": "nonsense"})


def test_parse_round_trips_every_client_message() -> None:
    for message in (Create(cwd="/w"), Push(text="hi"), Approval(execution_id="a", approved=True)):
        assert parse_client_message(decode(encode(message))) == message


def test_event_frame_keeps_the_event_and_names_the_session() -> None:
    frame = event_frame("01ABC", TextDelta(text="hello"))
    assert frame == {"type": "text_delta", "text": "hello", "session": "01ABC"}


# ---- autonomy: invariant 5 ------------------------------------------------------


def test_full_auto_is_refused_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STCODE_SANDBOX", raising=False)
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    with pytest.raises(AutonomyRefused):
        guard_autonomy("full-auto")


def test_every_other_mode_is_allowed_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    for mode in ("plan", "suggest", "auto-edit"):
        guard_autonomy(mode)


def test_the_sandbox_flag_is_what_a_container_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STCODE_SANDBOX", "1")
    guard_autonomy("full-auto")


def test_a_daemon_refuses_to_bind_in_full_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal happens before the socket exists — after would make it a message."""
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    config = config_for(tmp_path)
    config.defaults.approval_mode = "full-auto"
    daemon = Daemon(config)

    with pytest.raises(AutonomyRefused):
        asyncio.run(daemon.start())
    assert not Path(config.daemon.socket).exists()


# ---- runner: approval correlation -----------------------------------------------


async def build_runner(config: GatewayConfig, workspace: Path, gateway: Any) -> SessionRunner:
    daemon = Daemon(config, gateway=gateway)
    return await daemon.create_session(cwd=workspace)


@asynctest
async def test_approval_is_request_response(workspace: Path, tmp_path: Path) -> None:
    runner = await build_runner(config_for(tmp_path), workspace, ScriptedGateway([]))
    sink: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
    runner.subscribe(sink)

    request = ApprovalRequest(
        tool_name="bash", permission=ToolPermission.EXECUTE, execution_id="ab12"
    )
    pending = asyncio.create_task(runner.request_approval(request))
    frame = await asyncio.wait_for(sink.get(), 1)

    assert frame["type"] == "approval_request"
    assert frame["execution_id"] == "ab12"
    assert frame["tool"] == "bash"
    # The turn is genuinely parked: nothing resolves until a client answers.
    assert not pending.done()

    assert runner.resolve("ab12", True) is True
    assert await asyncio.wait_for(pending, 1) is True
    await runner.aclose()


@asynctest
async def test_a_question_gets_an_execution_id_the_daemon_mints(
    workspace: Path, tmp_path: Path
) -> None:
    runner = await build_runner(config_for(tmp_path), workspace, ScriptedGateway([]))
    sink: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
    runner.subscribe(sink)

    pending = asyncio.create_task(runner.ask(Question(question="Postgres or SQLite?")))
    frame = await asyncio.wait_for(sink.get(), 1)

    assert frame["type"] == "question" and frame["execution_id"]
    runner.resolve(frame["execution_id"], "Postgres")
    assert await asyncio.wait_for(pending, 1) == "Postgres"
    await runner.aclose()


@asynctest
async def test_no_client_attached_says_so_rather_than_claiming_a_refusal(
    workspace: Path, tmp_path: Path
) -> None:
    """A headless run must not be told a human declined — it would act on the lie."""
    runner = await build_runner(config_for(tmp_path), workspace, ScriptedGateway([]))
    request = ApprovalRequest(tool_name="bash", permission=ToolPermission.EXECUTE, execution_id="x")

    with pytest.raises(Exception, match="No client is attached"):
        await runner.request_approval(request)
    await runner.aclose()


@asynctest
async def test_the_last_client_leaving_abandons_the_open_request(
    workspace: Path, tmp_path: Path
) -> None:
    """Otherwise the turn waits forever on a Future nobody can resolve."""
    runner = await build_runner(config_for(tmp_path), workspace, ScriptedGateway([]))
    sink: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
    runner.subscribe(sink)

    request = ApprovalRequest(tool_name="bash", permission=ToolPermission.EXECUTE, execution_id="y")
    pending = asyncio.create_task(runner.request_approval(request))
    await asyncio.wait_for(sink.get(), 1)

    runner.unsubscribe(sink)
    with pytest.raises(Exception, match="abandoned"):
        await asyncio.wait_for(pending, 1)
    await runner.aclose()


@asynctest
async def test_an_open_request_is_replayed_to_whoever_attaches_next(
    workspace: Path, tmp_path: Path
) -> None:
    runner = await build_runner(config_for(tmp_path), workspace, ScriptedGateway([]))
    first: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
    runner.subscribe(first)
    second: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
    runner.subscribe(second)

    request = ApprovalRequest(tool_name="bash", permission=ToolPermission.EXECUTE, execution_id="z")
    pending = asyncio.create_task(runner.request_approval(request))
    await asyncio.wait_for(first.get(), 1)

    assert [frame["execution_id"] for frame in runner.open_requests()] == ["z"]
    # A second watcher answering is just as good as the one that was asked.
    runner.resolve("z", False)
    assert await asyncio.wait_for(pending, 1) is False
    await runner.aclose()


# ---- end to end, over a real socket ---------------------------------------------


class Harnessed:
    """A daemon on a real unix socket plus a connected client, torn down together."""

    def __init__(self, config: GatewayConfig, gateway: ScriptedGateway) -> None:
        self.config = config
        self.gateway = gateway
        self.daemon = Daemon(config, gateway=gateway)  # type: ignore[arg-type]

    async def __aenter__(self) -> "Harnessed":
        await self.daemon.start()
        return self

    async def client(self) -> DaemonClient:
        return await DaemonClient.connect(self.config)

    async def __aexit__(self, *_exc: Any) -> None:
        await self.daemon.aclose()


@asynctest
async def test_a_turn_streams_over_the_socket(workspace: Path, tmp_path: Path) -> None:
    gateway = ScriptedGateway([_text("Hello", " world")])
    async with Harnessed(config_for(tmp_path), gateway) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        assert info["id"] and info["approval_mode"] == "auto-edit"

        await client.push("hi")
        frames = await collect(client, until="turn_finished")

        assert types_of(frames) == ["text_delta", "text_delta", "turn_finished"]
        assert "".join(f["text"] for f in frames[:2]) == "Hello world"
        # The full Usage crosses the wire, not a two-field sketch: the cache counts are
        # the evidence for the prompt-caching claim.
        assert frames[-1]["usage"]["input_tokens"] == 10
        assert "cache_read_input_tokens" in frames[-1]["usage"]
        assert all(frame["session"] == info["id"] for frame in frames)
        await client.aclose()


@asynctest
async def test_tool_calls_reach_the_client(workspace: Path, tmp_path: Path) -> None:
    (workspace / "hello.txt").write_text("the file body")
    gateway = ScriptedGateway(
        [_call("c1", "read", path="hello.txt"), _text("It says: the file body")]
    )
    async with Harnessed(config_for(tmp_path), gateway) as env:
        client = await env.client()
        await client.create(cwd=workspace)
        await client.push("read hello.txt")
        frames = await collect(client, until="turn_finished")

        started = [f for f in frames if f["type"] == "tool_started"]
        finished = [f for f in frames if f["type"] == "tool_finished"]
        assert [f["name"] for f in started] == ["read"]
        assert finished[0]["ok"] is True and "the file body" in finished[0]["preview"]
        await client.aclose()


@asynctest
async def test_detach_does_not_kill_the_agent(workspace: Path, tmp_path: Path) -> None:
    """The whole reason the daemon exists (§8 point 2)."""
    gateway = ScriptedGateway([_text("finished after you left")])
    gateway.gate = asyncio.Event()

    async with Harnessed(config_for(tmp_path), gateway) as env:
        first = await env.client()
        info = await first.create(cwd=workspace)
        await first.push("do the thing")
        await asyncio.sleep(0.05)  # let the turn start and block on the gate

        await first.aclose()  # the client drops mid-turn
        runner = env.daemon.sessions[info["id"]]
        # The daemon learns about it when the read loop sees EOF, one tick later — a
        # dropped client is noticed, not announced.
        await until_true(lambda: runner.watchers == 0)
        assert runner.watchers == 0

        gateway.gate.set()  # the model answers to nobody
        for _ in range(50):
            if not runner.agent.busy:
                break
            await asyncio.sleep(0.02)

        # The events landed in the session, which is what makes re-attach lossless.
        kinds = [record["type"] for record in runner.agent.session.records()]
        assert "assistant" in kinds
        assert any(
            record.get("content") == "finished after you left"
            for record in runner.agent.session.records()
        )


@asynctest
async def test_reattach_replays_then_joins_the_live_stream(
    workspace: Path, tmp_path: Path
) -> None:
    gateway = ScriptedGateway([_text("first answer"), _text("second answer")])
    async with Harnessed(config_for(tmp_path), gateway) as env:
        first = await env.client()
        info = await first.create(cwd=workspace)
        await first.push("one")
        await collect(first, until="turn_finished")
        await first.aclose()

        second = await env.client()
        attached = await second.attach(info["id"])
        assert attached["id"] == info["id"]

        history = await second.next_event(2)
        assert history["type"] == "history"
        contents = [record.get("content") for record in history["records"]]
        assert "one" in contents and "first answer" in contents

        # …and the live stream continues on the same connection.
        await second.push("two")
        frames = await collect(second, until="turn_finished")
        assert "".join(f["text"] for f in frames if f["type"] == "text_delta") == "second answer"
        await second.aclose()


@asynctest
async def test_two_clients_watch_one_session(workspace: Path, tmp_path: Path) -> None:
    gateway = ScriptedGateway([_text("shared")])
    async with Harnessed(config_for(tmp_path), gateway) as env:
        first = await env.client()
        info = await first.create(cwd=workspace)
        second = await env.client()
        await second.attach(info["id"], replay=False)

        await first.push("go")
        a = await collect(first, until="turn_finished")
        b = await collect(second, until="turn_finished")
        assert "text_delta" in types_of(a) and "text_delta" in types_of(b)
        await first.aclose()
        await second.aclose()


@asynctest
async def test_a_push_mid_turn_queues_for_the_next_one(workspace: Path, tmp_path: Path) -> None:
    """It never splices into the turn in flight — that is why push and events are split."""
    gateway = ScriptedGateway([_text("one"), _text("two")])
    gateway.gate = asyncio.Event()

    async with Harnessed(config_for(tmp_path), gateway) as env:
        client = await env.client()
        await client.create(cwd=workspace)
        await client.push("first")
        await asyncio.sleep(0.05)
        await client.push("second")  # arrives while the first turn is blocked
        gateway.gate.set()

        first_turn = await collect(client, until="turn_finished")
        second_turn = await collect(client, until="turn_finished")
        assert "".join(f["text"] for f in first_turn if f["type"] == "text_delta") == "one"
        assert "".join(f["text"] for f in second_turn if f["type"] == "text_delta") == "two"
        await client.aclose()


@asynctest
async def test_a_real_tool_gate_reaches_the_client_and_back(
    workspace: Path, tmp_path: Path
) -> None:
    """The whole chain, not just the correlator: a `write` under `suggest` goes through
    `Tool.invoke` → `Runtime.request_approval` → the runner → the socket → back."""
    config = config_for(tmp_path)
    config.defaults.approval_mode = "suggest"
    gateway = ScriptedGateway(
        [_call("c1", "write", path="new.txt", content="written"), _text("done")]
    )

    async with Harnessed(config, gateway) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        assert info["approval_mode"] == "suggest"
        await client.push("write the file")

        request = None
        async with aclosing(client.events()) as stream:
            async for frame in stream:
                if frame["type"] == "approval_request":
                    request = frame
                    break
        assert request is not None and request["tool"] == "write"
        assert request["arguments"]["path"] == "new.txt"
        assert not (workspace / "new.txt").exists()  # still parked, nothing written yet

        await client.approve(request["execution_id"], True)
        frames = await collect(client, until="turn_finished")
        finished = [f for f in frames if f["type"] == "tool_finished"]
        assert finished and finished[0]["ok"] is True
        assert (workspace / "new.txt").read_text() == "written"
        await client.aclose()


@asynctest
async def test_denying_reaches_the_tool_as_a_denial(workspace: Path, tmp_path: Path) -> None:
    config = config_for(tmp_path)
    config.defaults.approval_mode = "suggest"
    gateway = ScriptedGateway(
        [_call("c1", "write", path="nope.txt", content="x"), _text("understood")]
    )

    async with Harnessed(config, gateway) as env:
        client = await env.client()
        await client.create(cwd=workspace)
        await client.push("write it")

        request = None
        async with aclosing(client.events()) as stream:
            async for frame in stream:
                if frame["type"] == "approval_request":
                    request = frame
                    break
        assert request is not None

        await client.approve(request["execution_id"], False)
        frames = await collect(client, until="turn_finished")
        finished = [f for f in frames if f["type"] == "tool_finished"]
        assert finished and finished[0]["ok"] is False
        assert not (workspace / "nope.txt").exists()
        await client.aclose()


@asynctest
async def test_sessions_lists_what_the_daemon_holds(workspace: Path, tmp_path: Path) -> None:
    async with Harnessed(config_for(tmp_path), ScriptedGateway([])) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        rows = await client.sessions()
        row = next(r for r in rows if r["id"] == info["id"])
        assert row["live"] is True and row["cwd"] == str(workspace)
        await client.aclose()


@asynctest
async def test_a_bad_message_is_diagnosed_not_fatal(workspace: Path, tmp_path: Path) -> None:
    async with Harnessed(config_for(tmp_path), ScriptedGateway([_text("still here")])) as env:
        client = await env.client()
        await client.create(cwd=workspace)

        client._writer.write(b"{not json}\n")
        await client._writer.drain()
        error = await client.next_event(2)
        assert error["type"] == "error"

        # The connection survived it.
        await client.push("hi")
        frames = await collect(client, until="turn_finished")
        assert "turn_finished" in types_of(frames)
        await client.aclose()


@asynctest
async def test_set_mode_reaches_the_live_agent(workspace: Path, tmp_path: Path) -> None:
    async with Harnessed(config_for(tmp_path), ScriptedGateway([])) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        await client.set_mode("plan")
        frame = await client.next_event(2)
        assert frame["approval_mode"] == "plan"
        assert env.daemon.sessions[info["id"]].agent.harness.approval_mode == "plan"
        await client.aclose()


@asynctest
async def test_set_mode_cannot_reach_full_auto_on_the_host(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`create` is not the only door a mode comes through. Cycling to `full-auto`
    mid-session would otherwise be exactly the override flag rule 5 says never exists."""
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    async with Harnessed(config_for(tmp_path), ScriptedGateway([])) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        await client.set_mode("full-auto")

        frame = await client.next_event(2)
        assert frame["type"] == "error" and "full-auto" in frame["message"]
        # …and the client is told what the mode actually is, so its status line cannot
        # end up showing one that was refused.
        echoed = await client.next_event(2)
        assert echoed["type"] == "session" and echoed["approval_mode"] == "auto-edit"
        assert env.daemon.sessions[info["id"]].agent.harness.approval_mode == "auto-edit"
        await client.aclose()


@asynctest
async def test_interrupt_ends_the_turn(workspace: Path, tmp_path: Path) -> None:
    """Interrupt is the way to actually stop an agent — `push` never is (§8 point 3)."""
    gateway = ScriptedGateway([_call("c1", "read", path="missing.txt"), _text("never reached")])
    gateway.gate = asyncio.Event()

    async with Harnessed(config_for(tmp_path), gateway) as env:
        client = await env.client()
        info = await client.create(cwd=workspace)
        await client.push("go")
        # Gate the model so the interrupt is guaranteed to land *during* the turn. Sent
        # before the turn starts it would be cleared at the top of `_run_turn`, and the
        # test would pass or hang depending on scheduling.
        await until_true(lambda: env.daemon.sessions[info["id"]].agent.busy)
        await client.interrupt()
        gateway.gate.set()

        frames = await collect(client, until="agent_failed", limit=20)
        assert frames[-1]["type"] == "agent_failed"
        assert "Interrupt" in frames[-1]["message"]
        # Terminal for the turn only: the session is still held and still usable.
        assert info["id"] in env.daemon.sessions
        await client.aclose()


@asynctest
async def test_a_stale_socket_file_is_cleared(tmp_path: Path) -> None:
    """A crashed daemon leaves the path behind; the next one must not refuse to start."""
    config = config_for(tmp_path)
    Path(config.daemon.socket).parent.mkdir(parents=True, exist_ok=True)
    Path(config.daemon.socket).write_text("debris")

    daemon = Daemon(config, gateway=ScriptedGateway([]))  # type: ignore[arg-type]
    await daemon.start()
    await daemon.aclose()
