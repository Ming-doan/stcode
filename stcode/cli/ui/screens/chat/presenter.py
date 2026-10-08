"""Incremental transcript presentation; owns every handle into rendered entries."""

from __future__ import annotations
import contextlib
from typing import Any
from stcode.cli import labels
from stcode.cli.ui.components.banner import Banner
from stcode.cli.ui.components.transcript import (
    Message,
    Thinking,
    Progress,
    ToolCall,
    Notice,
    PlatformNote,
    Transcript,
)

STREAM_KINDS = ("assistant", "thinking", "progress")
_OPEN_STREAM = {
    "assistant": lambda: Message("assistant"),
    "thinking": Thinking,
    "progress": Progress,
}


class ChatPresenter:
    def __init__(self, transcript: Transcript, banner: Banner) -> None:
        self.transcript = transcript
        self.banner = banner
        self.streams: dict[tuple[str, str], Message | Thinking | Progress] = {}
        self.tools: dict[tuple[str, str], ToolCall] = {}

    def clear(self) -> None:
        self.transcript.clear()
        self.streams.clear()
        self.tools.clear()
        self.settle()

    def render(self, frame: dict[str, Any]) -> None:
        agent = str(frame.get("agent", ""))
        match frame.get("type"):
            case "text_delta":
                self.stream_into("assistant", str(frame.get("text", "")), agent)
            case "reasoning_delta":
                self.stream_into("thinking", str(frame.get("text", "")), agent)
            case "tool_started":
                self.tool_started(frame, agent)
            case "tool_finished":
                self.tool_finished(frame, agent)
            case "turn_finished":
                self.end_streams(agent)
                if not agent:
                    self.note(labels.turn_usage(dict(frame.get("usage", {}))))
            case "agent_failed":
                self.end_streams(agent)
                self.error(str(frame.get("message", "")), agent=agent)
            case "supervisor":
                self.end_streams(agent)
                self.note(labels.supervisor_nudge(str(frame.get("text", ""))))
            case "progress":
                self.stream_into("progress", str(frame.get("text", "")), agent)
            case "history":
                self.replay(frame)
            case "error":
                if frame.get("level") == "warning":
                    self.warn(str(frame.get("message", "")))
                else:
                    self.error(str(frame.get("message", "")))

    def replay(self, frame: dict[str, Any]) -> None:
        """Render a re-attached session's transcript before its live stream arrives.

        Raw records, so this shows what happened rather than what the model was sent.
        """
        for record in frame.get("records", []):
            match record.get("type"):
                case "user":
                    self.transcript.add(Message("user", str(record.get("content", ""))))
                case "assistant" if record.get("content"):
                    self.transcript.add(
                        Message("assistant", str(record.get("content", "")))
                    )
                case "tool_call":
                    call = ToolCall(
                        str(record.get("name", "")), dict(record.get("arguments", {}))
                    )
                    self.transcript.add(call)
                    self.tools[("", str(record.get("id", "")))] = call
                case "tool_result":
                    call = self.tools.pop(("", str(record.get("id", ""))), None)
                    if call is not None:
                        call.finish(
                            ok=not record.get("is_error", False),
                            preview=str(record.get("content", "")),
                        )
                case "supervisor":
                    self.note(labels.supervisor_nudge(str(record.get("content", ""))))
                case "inbox":
                    self.note(
                        labels.inbox_message(
                            str(record.get("from", "")),
                            str(record.get("subject", "")),
                            [str(ref) for ref in record.get("refs", [])],
                        )
                    )
                case "error":
                    self.error(str(record.get("message", "")))
        self.settle()

    def tool_started(self, frame: dict[str, Any], agent: str) -> None:
        self.end_streams(agent)
        call = ToolCall(str(frame.get("name", "")), dict(frame.get("arguments", {})))
        self.transcript.add(call, agent=agent)
        self.tools[(agent, str(frame.get("id", "")))] = call
        self.settle()

    def tool_finished(self, frame: dict[str, Any], agent: str) -> None:
        call = self.tools.pop((agent, str(frame.get("id", ""))), None)
        if call is None:
            # A result with no box: the client attached mid-turn. One box saying what
            # came back beats silently dropping it.
            call = ToolCall(str(frame.get("name", "")), {})
            self.transcript.add(call, agent=agent)
        call.finish(
            ok=bool(frame.get("ok", True)), preview=str(frame.get("preview", ""))
        )

    def stream_into(self, kind: str, text: str, agent: str) -> None:
        """Append a delta, opening a new block when the kind of delta changes.

        Keyed by `(agent, kind)`: deltas carry no boundaries, so a different kind
        arriving is the only signal a block ended — and two sub-agents running in
        parallel must not stream into each other's block.

        `progress` is one of the kinds for the same reason the other two are: a REPL
        cell streams a line at a time, and one entry per line is a screen of entries
        for one tool call.
        """
        key = (agent, kind)
        block = self.streams.get(key)
        if block is None:
            for other in STREAM_KINDS:
                if other != kind:
                    self.close_stream((agent, other))
            block = _OPEN_STREAM[kind]()
            self.transcript.add(block, agent=agent)
            self.streams[key] = block
        block.append(text)
        self.transcript.scroll_end(animate=False)
        self.settle()

    def end_streams(self, agent: str = "") -> None:
        for kind in STREAM_KINDS:
            self.close_stream((agent, kind))

    def close_stream(self, key: tuple[str, str]) -> None:
        block = self.streams.pop(key, None)
        if block is not None and not block.body.strip():
            block.remove()

    def settle(self) -> None:
        """Hide the banner once the transcript has outgrown the screen.

        After the refresh, not during it: mounting is queued, so `max_scroll_y` read
        immediately after adding an entry is the value from *before* that entry existed
        — and the banner would sit there through a whole conversation.
        """
        self.transcript.call_after_refresh(self.settle_now)

    def settle_now(self) -> None:
        with contextlib.suppress(Exception):
            self.banner.follow(transcript_scrolls=self.transcript.scrolls)

    def note(self, body: str) -> None:
        """What the platform did, as a rule across the screen."""
        self.end_streams()
        self.transcript.add(PlatformNote(body))
        self.settle()

    def warn(self, body: str) -> None:
        self.transcript.add(Notice(body, level="warning"))
        self.settle()

    def error(self, body: str, *, agent: str = "") -> None:
        self.end_streams(agent)
        self.transcript.add(Notice(body, level="error"), agent=agent)
        self.settle()
