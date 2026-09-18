"""
Session — one append-only JSONL file, three readers.

`~/.stcode/sessions/<id>.jsonl`. Every event goes in, including the ones the model
never sees:

* `messages()` — history for the gateway, folded into provider `Message`s.
* the file itself — the trajectory. Agent → tool → sub-agent leaves no natural stack
  trace, so this *is* the stack trace.
* `tail()` — raw records for the supervisor, which needs what the model is not shown.

**Append-only, and that is load-bearing.** No rewriting, no branch, no fork: `cp
session.jsonl` is the branching feature. It is also what makes `append()` one `write`
+ `flush`, and so safe to call synchronously from the agent loop — the one exception to
"no blocking I/O in the loop", because ~20µs of buffered write beats the machinery to
avoid it.

Swapping to a live database later replaces this class; `Agent` sees the same four
methods. Do not abstract for that now — a class with four methods **is** the
abstraction.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from stcode.core.common.ids import new_id
from stcode.core.providers.types import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

DEFAULT_SESSION_DIR = "~/.stcode/sessions"

MODEL_VISIBLE = frozenset({"user", "assistant", "tool_call", "tool_result", "supervisor", "inbox"})
"""Record types `messages()` folds into history. `meta`, `usage` and `error` are for
the human reading the trajectory and for the supervisor — sending the model its own
token counts is paying to tell it something it cannot act on."""


def sessions_dir(directory: str | Path | None = None) -> Path:
    return Path(os.path.expanduser(str(directory or DEFAULT_SESSION_DIR)))


class Session:
    """An append-only JSONL transcript of one agent's work."""

    def __init__(self, path: Path, *, records: list[dict[str, Any]] | None = None) -> None:
        """Open `path`, adopting whatever is already in it.

        Appending to a file whose earlier content was never loaded breaks the premise:
        `messages()` would describe a shorter conversation than the file does, and the
        trajectory would read as a continuation the model never saw. Pointing at an
        existing file means resuming it — the file is the truth, not the object. Pass
        `records` only when you have already read them.
        """
        self.path = path
        self.id = path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if records is None:
            records = list(read_records(path)) if path.is_file() else []
        self._records: list[dict[str, Any]] = records
        # Line-buffered and held open: a turn with twenty tool calls produces sixty
        # events, and open/close per event is not worth paying.
        self._file = self.path.open("a", encoding="utf-8", buffering=1)

    # ---- construction ----

    @classmethod
    def create(
        cls,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        model: str = "",
        directory: str | Path | None = None,
        **meta: Any,
    ) -> "Session":
        """Start a new session, writing the `meta` record that `list()` reads back."""
        root = sessions_dir(directory)
        session = cls(root / f"{new_id()}.jsonl")
        session.append(
            type="meta",
            id=session.id,
            cwd=str(Path(cwd).expanduser() if cwd else Path.cwd()),
            role=role,
            model=model,
            **meta,
        )
        return session

    @classmethod
    def resume(cls, session_id: str, *, directory: str | Path | None = None) -> "Session":
        """Reopen an existing session and keep appending to it."""
        path = sessions_dir(directory) / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"No session {session_id!r} in {path.parent}.")
        return cls(path)

    @classmethod
    def list(cls, limit: int = 20, *, directory: str | Path | None = None) -> list[dict[str, Any]]:
        """The `meta` record of each session, newest first.

        A directory listing and one line per file. No index, no SQLite: ULID filenames
        already sort by time, so the newest N are the last N names.
        """
        root = sessions_dir(directory)
        if not root.is_dir():
            return []
        summaries: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.jsonl"), reverse=True)[:limit]:
            meta = _first_meta(path)
            meta.setdefault("id", path.stem)
            meta["path"] = str(path)
            summaries.append(meta)
        return summaries

    def child(self, name: str) -> "Session":
        """A separate session file for a sub-agent, linked back to this one.

        A separate *file*: interleaving a child's tool calls into the parent's
        transcript would make `messages()` produce a conversation neither agent had.
        The link is one field in the child's `meta`, enough to reassemble the tree.
        """
        return Session.create(
            cwd=self.meta().get("cwd"),
            role=self.meta().get("role", ""),
            model=self.meta().get("model", ""),
            directory=self.path.parent,
            parent=self.id,
            agent_name=name,
        )

    # ---- writing ----

    def append(self, **record: Any) -> dict[str, Any]:
        """Append one record. Synchronous and flushed — see the module docstring.

        `type` is required; `ts` is stamped here so no caller has to remember to.
        """
        if "type" not in record:
            raise ValueError("a session record needs a `type`")
        record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        self._records.append(record)
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    # ---- reading ----

    def meta(self) -> dict[str, Any]:
        for record in self._records:
            if record.get("type") == "meta":
                return record
        return {}

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def tail(self, count: int = 30) -> list[dict[str, Any]]:
        """The last `count` raw records — the supervisor's input."""
        return list(self._records[-count:]) if count > 0 else []

    def messages(self) -> list[Message]:
        """Fold the transcript into what the gateway sends.

        Three foldings, each demanded by the wire format:

        * an `assistant` record plus the `tool_call`s after it become **one** assistant
          message of `ToolUseBlock`s — providers reject a tool call not attached to the
          assistant turn that made it;
        * consecutive `tool_result`s become **one** user message of `ToolResultBlock`s,
          because a turn's parallel calls are answered together;
        * `supervisor` and `inbox` become prefixed user messages — neither is a role any
          provider knows, and the model has to be told who is talking.
        """
        messages: list[Message] = []
        assistant_blocks: list[ContentBlock] = []
        result_blocks: list[ContentBlock] = []

        def flush_assistant() -> None:
            nonlocal assistant_blocks
            if assistant_blocks:
                messages.append(Message(role="assistant", content=assistant_blocks))
                assistant_blocks = []

        def flush_results() -> None:
            nonlocal result_blocks
            if result_blocks:
                messages.append(Message(role="user", content=result_blocks))
                result_blocks = []

        for record in self._records:
            kind = record.get("type")
            if kind not in MODEL_VISIBLE:
                continue

            if kind == "tool_call":
                flush_results()
                assistant_blocks.append(
                    ToolUseBlock(
                        id=str(record.get("id", "")),
                        name=str(record.get("name", "")),
                        input=dict(record.get("arguments") or {}),
                    )
                )
                continue

            if kind == "tool_result":
                flush_assistant()
                result_blocks.append(
                    ToolResultBlock(
                        tool_use_id=str(record.get("id", "")),
                        content=str(record.get("content", "")),
                        is_error=bool(record.get("is_error", False)),
                    )
                )
                continue

            flush_assistant()
            flush_results()
            if kind == "assistant":
                text = str(record.get("content", ""))
                if text:
                    assistant_blocks.append(TextBlock(text=text))
                continue
            messages.append(Message(role="user", content=_render_user(record)))

        flush_assistant()
        flush_results()
        return messages

    # ---- lifecycle ----

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Session({self.id}, {len(self._records)} records)"


def _render_user(record: dict[str, Any]) -> str:
    """A `user`, `supervisor` or `inbox` record as text the model can act on.

    The prefixes matter: a nudge that reads like the user asking for something gets
    treated as a new instruction, while one that names its source gets treated as the
    correction it is. An inbox message carries `refs`, so the paths are the payload.
    """
    kind = record.get("type")
    if kind == "supervisor":
        return f"[supervisor] {record.get('content', '')}"
    if kind == "inbox":
        refs = record.get("refs") or []
        lines = [f"[message from {record.get('from', 'unknown')}] {record.get('subject', '')}"]
        if record.get("body"):
            lines.append(str(record["body"]))
        if refs:
            lines.append("Refs: " + ", ".join(str(ref) for ref in refs))
        return "\n".join(lines)
    return str(record.get("content", ""))


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Every well-formed record in a session file.

    A truncated final line (the process died mid-write) is skipped, not raised on: a
    session you cannot open is one whose trajectory you cannot read, which is exactly
    when you most need to.
    """
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _first_meta(path: Path) -> dict[str, Any]:
    for record in read_records(path):
        if record.get("type") == "meta":
            return record
        break  # meta is always first; anything else means there is none
    return {}


def prune(keep: int = 100, *, directory: str | Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` session files. Returns what was removed."""
    root = sessions_dir(directory)
    if not root.is_dir():
        return []
    stale: Sequence[Path] = sorted(root.glob("*.jsonl"), reverse=True)[keep:]
    removed = []
    for path in stale:
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


__all__ = [
    "DEFAULT_SESSION_DIR",
    "MODEL_VISIBLE",
    "Session",
    "new_id",
    "prune",
    "read_records",
    "sessions_dir",
]
