"""
The wire — JSONL, one message per line, same framing on unix and TCP.

Everything crossing a process boundary is shaped here and nowhere else.

* **One line, flushed** — the same discipline as the session file and the REPL worker.
  Nothing on the wire may hold a raw newline, so the encoder is not optional.
* **Agent events are not re-wrapped.** `core/agent/events.py` already carries primitives
  with the right `type`, so a `TextDelta` is dumped as-is plus a `session` field.
* `turn_finished` carries the **full four-field `Usage`** — the cache counts are the
  evidence for the prompt-caching claim.
* `set_mode` and `set_meta` exist, so `/mode`, `/model` and `/effort` reach the live
  session rather than only later ones.
* `info` exists because a client cannot answer "which skills are there" for itself —
  in `--daemonless` the workspace is on the daemon's machine, not the terminal's.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from stcode.core.agent.events import AgentEvent
from stcode.core.harness.approvals import ApprovalMode
from stcode.core.harness.tools.base import ApprovalRequest, Question
from stcode.core.providers.types import ReasoningEffort


class ProtocolError(Exception):
    """A line that is not a message this daemon understands."""


# ---- client → daemon ------------------------------------------------------------
#
# Every message that acts on a session takes an optional `session` id. A connection
# remembers the last session it created or attached to and uses that when the field is
# empty, which is what §11.1's `{"type":"push","text":"…"}` assumes; the field is there
# because one connection may attach to several sessions at once and then has to say
# which one it means.


class Create(BaseModel):
    """Start a new session. The daemon replies with `session` and attaches this client."""

    type: Literal["create"] = "create"
    cwd: str = ""
    role: str = ""
    approval_mode: ApprovalMode | None = None


class Attach(BaseModel):
    """Join a session. Several clients may attach to one at the same time.

    An id the daemon is not holding is resumed from disk, which is what makes
    `stcode --resume` a protocol message rather than a second code path.
    """

    type: Literal["attach"] = "attach"
    session: str = ""
    replay: bool = True


class Detach(BaseModel):
    """Stop receiving this session's events. **Does not stop the agent** — that is the
    entire reason the daemon exists (§8 point 2)."""

    type: Literal["detach"] = "detach"
    session: str = ""


class Sessions(BaseModel):
    """What this daemon is holding, plus what is on disk."""

    type: Literal["sessions"] = "sessions"
    limit: int = 20


class Push(BaseModel):
    """Queue a message. Arriving mid-turn, it waits for the next turn (§8 point 3)."""

    type: Literal["push"] = "push"
    text: str
    session: str = ""


class Interrupt(BaseModel):
    """Stop the turn in flight at its next checkpoint."""

    type: Literal["interrupt"] = "interrupt"
    session: str = ""


class Approval(BaseModel):
    """The answer to an `approval_request`, correlated by `execution_id`."""

    type: Literal["approval"] = "approval"
    execution_id: str
    approved: bool = False
    session: str = ""


class Answer(BaseModel):
    """The answer to a `question`, correlated by `execution_id`."""

    type: Literal["answer"] = "answer"
    execution_id: str
    text: str = ""
    session: str = ""


class SetMode(BaseModel):
    """Change a live session's approval mode."""

    type: Literal["set_mode"] = "set_mode"
    mode: ApprovalMode
    session: str = ""


class SetMeta(BaseModel):
    """Change a live session's model, provider or reasoning effort.

    A **patch**: an unset field is left alone, so `/effort` does not have to restate
    the model. It lands as a `meta` record appended to the session, which is why the
    change is in the transcript and nothing is rewritten.
    """

    type: Literal["set_meta"] = "set_meta"
    model: str | None = None
    provider: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    session: str = ""

    def fields(self) -> dict[str, Any]:
        """Just the settings that were actually given."""
        return {
            key: value
            for key, value in (
                ("model", self.model),
                ("provider", self.provider),
                ("reasoning_effort", self.reasoning_effort),
            )
            if value
        }


class Info(BaseModel):
    """Ask what this daemon's machine has: skills, MCP servers, tools, paths."""

    type: Literal["info"] = "info"
    session: str = ""


class GetConfig(BaseModel):
    """Ask for the daemon's own `config.toml`, as it is holding it.

    A `--daemonless` terminal shows settings that belong to the daemon's machine, so it
    has to ask rather than read its own file — the local one describes a different
    agent. Literal `api_key`s are redacted on the way out.
    """

    type: Literal["get_config"] = "get_config"


CONFIGURABLE_DEFAULTS = ("provider", "model", "reasoning_effort", "approval_mode")
"""The only keys `set_config` accepts, and the reason it is safe to accept any.

Everything else in the file — a key, a `base_url`, a routing tier, a concurrency cap —
is how the *operator* provisioned this daemon, usually from an environment variable
rather than from the file at all. A terminal that could rewrite those means one `/model`
on the wrong tab silently repoints a fleet at a different endpoint. So they travel one
way: shown, redacted, and not accepted back.
"""


class SetConfig(BaseModel):
    """Write `[defaults]` to the daemon's config file and reload the gateway.

    A patch, and a narrow one: anything outside `CONFIGURABLE_DEFAULTS` is dropped
    rather than refused, because a client sending a whole config back is asking for the
    four keys it is allowed to change and should not have to know which those are.

    Answered with the same `config` frame `get_config` sends, so a client never shows a
    change it only asked for.
    """

    type: Literal["set_config"] = "set_config"
    defaults: dict[str, Any] = Field(default_factory=dict)

    def patch(self) -> dict[str, Any]:
        """Just the keys this message is allowed to change, and only those that are set."""
        return {
            key: value
            for key, value in self.defaults.items()
            if key in CONFIGURABLE_DEFAULTS and value not in (None, "")
        }


ClientMessage = Annotated[
    Union[
        Create,
        Attach,
        Detach,
        Sessions,
        Push,
        Interrupt,
        Approval,
        Answer,
        SetMode,
        SetMeta,
        Info,
        GetConfig,
        SetConfig,
    ],
    Field(discriminator="type"),
]

_CLIENT_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---- daemon → client ------------------------------------------------------------


class SessionOpened(BaseModel):
    """Answer to `create`, `attach`, `set_mode` and `set_meta`: what this session now is.

    The settings are the **effective** ones — `meta` merged with any overrides — because
    a status line showing what a session started as is a status line that lies after the
    first `/model`. `started` says whether the transcript exists yet: a session can be
    an id and nothing more until its first message.
    """

    type: Literal["session"] = "session"
    id: str
    cwd: str = ""
    role: str = ""
    model: str = ""
    provider: str = ""
    reasoning_effort: str = ""
    approval_mode: str = ""
    busy: bool = False
    started: bool = False


class SessionList(BaseModel):
    type: Literal["sessions"] = "sessions"
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class History(BaseModel):
    """The transcript so far, sent on attach before the live stream is joined.

    Raw session records rather than `Message`s: a re-attaching client wants what the
    trajectory holds — tool calls, errors, usage — not the folded model history.
    """

    type: Literal["history"] = "history"
    session: str = ""
    records: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalRequested(BaseModel):
    """A tool is waiting on a human. Answer with `approval` carrying this
    `execution_id`; whoever answers first decides."""

    type: Literal["approval_request"] = "approval_request"
    session: str = ""
    execution_id: str
    tool: str
    permission: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_name: str = "main"

    @classmethod
    def of(cls, session: str, request: ApprovalRequest) -> "ApprovalRequested":
        return cls(
            session=session,
            execution_id=request.execution_id,
            tool=request.tool_name,
            permission=str(request.permission),
            arguments=request.arguments,
            agent_name=request.agent_name,
        )


class QuestionAsked(BaseModel):
    """`ask_user_question` is waiting. Answer with `answer`.

    `execution_id` is minted by the daemon: `Question` has no field for one, unlike
    `ApprovalRequest`. Same correlation mechanism, one end of it supplied here.
    """

    type: Literal["question"] = "question"
    session: str = ""
    execution_id: str
    question: str
    header: str = ""
    options: list[str] = Field(default_factory=list)
    multi_select: bool = False

    @classmethod
    def of(cls, session: str, execution_id: str, question: Question) -> "QuestionAsked":
        return cls(
            session=session,
            execution_id=execution_id,
            question=question.question,
            header=question.header,
            options=question.options,
            multi_select=question.multi_select,
        )


class InfoReply(BaseModel):
    """Answer to `info`: what the **daemon's** machine has.

    Every field here is something a client cannot see for itself once the agent is in a
    container: the workspace, the config file, the skills that were discovered, the MCP
    servers that answered. In solo mode that distinction is invisible; in
    `--daemonless` it is the whole reason this message exists.
    """

    type: Literal["info"] = "info"
    session: str = ""
    version: str = ""
    daemon: str = ""
    cwd: str = ""
    role: str = ""
    config_path: str = ""
    session_path: str = ""
    session_dir: str = ""
    approval_mode: str = ""
    tools: list[str] = Field(default_factory=list)
    skills: list[dict[str, str]] = Field(default_factory=list)
    mcp: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    """This session's token totals, summed from its `usage` records — what `/token`
    shows. Here rather than as its own message because it is the same question `info`
    already answers: *what does the daemon know about this session that I cannot see?*
    """


class ConfigReply(BaseModel):
    """Answer to `get_config` and `set_config`: what this daemon is configured with.

    `path` is where it lives **on the daemon's disk**, which is the field that makes the
    difference visible in `--daemonless` — a client showing `~/.stcode/config.toml` while
    driving a container is a client lying about which machine it is describing.

    `writable` says whether `set_config` would land. False for a daemon whose config
    file it cannot write, so a client can grey the fields out instead of offering an
    edit that will fail.
    """

    type: Literal["config"] = "config"
    path: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    writable: bool = True


class Progress(BaseModel):
    """A line of progress from a long-running tool. Advisory: nothing is recorded."""

    type: Literal["progress"] = "progress"
    session: str = ""
    text: str = ""


class ErrorMessage(BaseModel):
    """A protocol- or daemon-level failure. Not `agent_failed`, which is a turn ending
    badly and is the agent's own event."""

    type: Literal["error"] = "error"
    session: str = ""
    message: str


ServerMessage = Union[
    SessionOpened,
    SessionList,
    History,
    ApprovalRequested,
    QuestionAsked,
    InfoReply,
    ConfigReply,
    Progress,
    ErrorMessage,
]


# ---- framing --------------------------------------------------------------------

STREAM_LIMIT = 64 * 1024 * 1024
"""How long one line may be, on both ends of every connection.

`asyncio`'s streams default to **64 KiB** per line, and a line over it raises
`ValueError` out of the read loop rather than arriving. The framing here is one message
per line, and `history` is one message carrying a whole transcript — so the default cap
means *a session stops being resumable the moment its file passes 64 KiB*, which is
about thirty tool calls. The frame is built in memory either way; this only says the
reader must be willing to receive what the writer was willing to send.
"""


def encode(message: BaseModel | dict[str, Any]) -> bytes:
    """One message, one line, UTF-8. `default=str` so a stray Path or datetime in a
    tool's arguments degrades to text instead of killing the connection."""
    payload = message.model_dump(mode="json") if isinstance(message, BaseModel) else message
    return (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """One line back to an object, or `ProtocolError`."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("a message must be a JSON object")
    return payload


def parse_client_message(payload: dict[str, Any]) -> ClientMessage:
    """Validate one decoded line as a client message.

    Errors name the `type` the client sent, because "field required" against a model
    the client has never heard of is not a diagnosis.
    """
    try:
        return _CLIENT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        kind = payload.get("type", "(missing)")
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'message'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ProtocolError(f"bad {kind!r} message: {detail}") from exc


def event_frame(session: str, event: AgentEvent, *, agent: str = "") -> dict[str, Any]:
    """An agent event as it goes on the wire: itself, plus where it came from.

    `agent` names the **sub-agent** that produced it and is omitted when there is none,
    so a frame without the field is the session's own agent. One extra field rather
    than a parallel set of sub-agent message types — the same reason agent events are
    not re-wrapped in the first place.
    """
    frame = {**event.model_dump(mode="json"), "session": session}
    if agent:
        frame["agent"] = agent
    return frame


__all__ = [
    "CONFIGURABLE_DEFAULTS",
    "STREAM_LIMIT",
    "Answer",
    "Approval",
    "ApprovalRequested",
    "Attach",
    "ClientMessage",
    "ConfigReply",
    "Create",
    "Detach",
    "ErrorMessage",
    "GetConfig",
    "History",
    "Info",
    "InfoReply",
    "Interrupt",
    "Progress",
    "ProtocolError",
    "Push",
    "QuestionAsked",
    "ServerMessage",
    "SessionList",
    "SessionOpened",
    "Sessions",
    "SetConfig",
    "SetMeta",
    "SetMode",
    "decode",
    "encode",
    "event_frame",
    "parse_client_message",
]
