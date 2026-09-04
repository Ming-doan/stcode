"""
Session — one append-only JSONL file, three readers.

`~/.stcode/sessions/<id>.jsonl`. Every event the agent produces goes in, including the
ones the model never sees, and three consumers read the same file:

* `messages()` — history for the gateway. Folds records into provider `Message`s.
* the file itself — the trajectory. Agent → tool → sub-agent leaves no natural stack
  trace, so this *is* the stack trace (CLAUDE.md §11).
* `tail()` — raw records for the supervisor (step 9), which counts repeats and error
  rates and needs the records the model is not shown.

**Append-only, and that is load-bearing** (CLAUDE.md §4 rule 4). No rewriting, no
branch, no fork, no leaf pointer: `cp session.jsonl` is the branching feature. It is
also what makes `append()` a single `write` + `flush` and therefore safe to call
synchronously from the agent loop — the one exception to "no blocking I/O in the loop",
because ~20µs of buffered write is cheaper than the machinery to avoid it.

Swapping to a live database later replaces this class; `Agent` sees the same four
methods. Do not write an abstraction for that now — a class with four methods **is**
the abstraction.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from stcode.core.providers.types import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

DEFAULT_SESSION_DIR = "~/.stcode/sessions"

MODEL_VISIBLE = frozenset({"user", "assistant", "tool_call", "tool_result", "supervisor", "inbox"})
"""Record types `messages()` folds into history. Everything else — `meta`, `usage`,
`error` — is for the human reading the trajectory and for the supervisor. Sending the
model its own token counts would be paying to tell it something it cannot act on."""

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


_last_id: tuple[int, int] = (0, 0)


def new_id() -> str:
    """A monotonic ULID: 48 bits of millisecond timestamp, 80 bits of randomness, base32.

    Sortable by creation time as a plain string, which is the entire reason to not use
    `uuid4`: `Session.list()` is a directory listing, and lexical order being time order
    means it needs no index and no metadata read to sort.

    Monotonic because "same millisecond" is not hypothetical — spawning three sub-agents
    in one turn does exactly that, and plain random suffixes would put them in an
    arbitrary order. Within a millisecond the randomness increments instead, which also
    covers the clock stepping backwards under NTP.
    """
    global _last_id
    stamp = int(time.time() * 1000)
    last_stamp, last_random = _last_id
    if stamp > last_stamp:
        _last_id = (stamp, secrets.randbits(80))
    else:
        _last_id = (last_stamp, last_random + 1)
    stamp, randomness = _last_id
    value = (stamp << 80) | (randomness & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def sessions_dir(directory: str | Path | None = None) -> Path:
    return Path(os.path.expanduser(str(directory or DEFAULT_SESSION_DIR)))


class Session:
    """An append-only JSONL transcript of one agent's work."""

    def __init__(self, path: Path, *, records: list[dict[str, Any]] | None = None) -> None:
        """Open `path`, adopting whatever is already in it.

        Reading the existing file matters more than it looks. The class's premise is one
        file serving three readers, and a `Session` that appends to a file whose earlier
        content it has not loaded breaks that on the spot: `messages()` would describe a
        shorter conversation than the file does, and the next run would look, to anyone
        reading the trajectory, like a continuation of the last one when the model never
        saw it. Pointing at an existing file therefore means resuming it — the file is
        the truth, not the object. Pass `records` only when you have already read them.
        """
        self.path = path
        self.id = path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if records is None:
            records = list(read_records(path)) if path.is_file() else []
        self._records: list[dict[str, Any]] = records
        # Line-buffered and held open: the alternative is an open/close per event, and
        # a turn with twenty tool calls produces sixty events.
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

        A directory listing and one line read per file. No index and no SQLite: ULID
        filenames already sort by time, so the newest N are the last N names, and only
        those get opened.
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

        A separate *file*, not a branch of this one: rule 4 forbids rewriting history,
        and interleaving a child's tool calls into the parent's transcript would make
        `messages()` produce a conversation neither agent actually had. The link is one
        field in the child's `meta` record, which is enough to reassemble the tree when
        reading a trajectory.
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
        """The last `count` raw records — the supervisor's input (step 9)."""
        return list(self._records[-count:]) if count > 0 else []

    def messages(self) -> list[Message]:
        """Fold the transcript into what the gateway sends.

        Three foldings, and each exists because the wire format demands it:

        * an `assistant` record plus the `tool_call` records that follow it become **one**
          assistant message carrying `ToolUseBlock`s — providers reject a tool call that
          is not attached to the assistant turn that made it;
        * consecutive `tool_result` records become **one** user message of
          `ToolResultBlock`s, because a turn's parallel calls are answered together;
        * `supervisor` and `inbox` become prefixed user messages, since neither is a role
          any provider knows and the model has to be told who is talking.
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
    """A `user`, `supervisor`, or `inbox` record as text the model can act on.

    The prefixes matter. A supervisor nudge that reads like the user asking for
    something gets treated as a new instruction; one that says where it came from gets
    treated as the correction it is. An inbox message carries `refs` rather than
    content by design (CLAUDE.md §2.3), so the paths are the payload.
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

    A truncated final line — the process died mid-write — is skipped rather than raised
    on. A session you cannot open is a session whose trajectory you cannot read, which
    is exactly when you most need to.
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
        break  # meta is always the first record; anything else means there is none
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
