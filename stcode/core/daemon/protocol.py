"""
The wire — JSONL, one message per line, same framing on unix and TCP.

Everything that crosses a process boundary is shaped here and nowhere else
(CLAUDE.md §5.1). Two rules the rest of the package leans on:

* **Framing is one line, flushed.** The same discipline as the session file and the
  REPL worker. `\\n` terminates a message, so nothing on the wire may contain a raw
  newline — `json.dumps` guarantees that, which is why the encoder is not optional.
* **Agent events are not re-wrapped.** `core/agent/events.py` already carries
  primitives with a `type` field that reads the way the wire should, so a `TextDelta`
  is dumped as-is with a `session` field added. A parallel set of wire models would be
  a translation layer whose only job is to be kept in sync.

Two deliberate differences from the sketch in EXPECTED.md §11.1, both recorded in
CLAUDE.md §8:

* `turn_finished` carries the full `Usage` (four fields) rather than `{"in","out"}`.
  The cache counts are the evidence for the prompt-caching claim, and dropping them on
  the wire would mean the one client that could show them cannot.
* `set_mode` exists. The TUI has had `/mode` since phase 0; without a message for it,
  cycling the approval mode would silently affect only sessions created afterwards.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from stcode.core.agent.events import AgentEvent
from stcode.core.harness.approvals import ApprovalMode
from stcode.core.harness.tools.base import ApprovalRequest, Question


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


ClientMessage = Annotated[
    Union[Create, Attach, Detach, Sessions, Push, Interrupt, Approval, Answer, SetMode],
    Field(discriminator="type"),
]

_CLIENT_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---- daemon → client ------------------------------------------------------------


class SessionOpened(BaseModel):
    """Answer to `create` and to `attach`: which session this connection is now on."""

    type: Literal["session"] = "session"
    id: str
    cwd: str = ""
    role: str = ""
    model: str = ""
    approval_mode: str = ""
    busy: bool = False


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
    SessionOpened, SessionList, History, ApprovalRequested, QuestionAsked, Progress, ErrorMessage
]


# ---- framing --------------------------------------------------------------------


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


def event_frame(session: str, event: AgentEvent) -> dict[str, Any]:
    """An agent event as it goes on the wire: itself, plus which session it came from."""
    return {**event.model_dump(mode="json"), "session": session}


__all__ = [
    "Answer",
    "Approval",
    "ApprovalRequested",
    "Attach",
    "ClientMessage",
    "Create",
    "Detach",
    "ErrorMessage",
    "History",
    "Interrupt",
    "Progress",
    "ProtocolError",
    "Push",
    "QuestionAsked",
    "ServerMessage",
    "SessionList",
    "SessionOpened",
    "Sessions",
    "SetMode",
    "decode",
    "encode",
    "event_frame",
    "parse_client_message",
]
