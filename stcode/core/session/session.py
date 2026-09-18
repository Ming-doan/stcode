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

Two consequences of that rule, both here:

* **`defer=True` holds the file back until the first message.** A session id can exist
  with nothing behind it, which is what lets the UI's `/clear` be "start another one"
  instead of a directory of empty transcripts.
* **A setting is changed by appending another `meta` record**, never by editing the
  first one. `meta()` is what the session started as; `overrides()` is what has been
  changed since. That is how `/model` reaches a running agent.

Swapping to a live database later replaces this class; `Agent` sees the same four
methods. Do not abstract for that now — a class with four methods **is** the
abstraction.
"""

from __future__ import annotations

import builtins
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO

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


META_BOOKKEEPING = frozenset({"type", "ts", "id", "parent", "agent_name"})
"""Keys a later `meta` record carries but that are not settings. `overrides()` drops
them, so a client can hand the whole dict to the gateway without sieving it first."""


def sessions_dir(directory: str | Path | None = None) -> Path:
    return Path(os.path.expanduser(str(directory or DEFAULT_SESSION_DIR)))


class Session:
    """An append-only JSONL transcript of one agent's work.

    `builtins.list[...]` in the annotations below is not decoration: this class has a
    classmethod called `list`, which shadows the builtin inside the class body, so a
    bare `list[dict[str, Any]]` on a method resolves to the classmethod and every
    annotation using it is quietly wrong. The name `Session.list()` is worth keeping;
    the four extra characters are the price.
    """

    def __init__(
        self,
        path: Path,
        *,
        records: builtins.list[dict[str, Any]] | None = None,
        defer: bool = False,
    ) -> None:
        """Open `path`, adopting whatever is already in it.

        Appending to a file whose earlier content was never loaded breaks the premise:
        `messages()` would describe a shorter conversation than the file does, and the
        trajectory would read as a continuation the model never saw. Pointing at an
        existing file means resuming it — the file is the truth, not the object. Pass
        `records` only when you have already read them.

        `defer=True` does not open or create anything. The first record that is not
        `meta` does both, and until then this is an id with a `meta` record behind it.
        """
        self.path = path
        self.id = path.stem
        if records is None:
            records = list(read_records(path)) if path.is_file() else []
        self._records: builtins.list[dict[str, Any]] = records
        self._held: builtins.list[dict[str, Any]] = []
        """Records appended before the file was opened. Written, in order, by `_start`."""

        self._file: TextIO | None = None
        if not defer:
            self._start()

    @property
    def started(self) -> bool:
        """Whether this session has a file. False only for a deferred one that nobody
        has said anything to."""
        return self._file is not None

    def _start(self) -> None:
        """Open the file and flush anything held back. Idempotent by the caller's check.

        Line-buffered and held open: a turn with twenty tool calls produces sixty
        events, and open/close per event is not worth paying.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", buffering=1)
        held, self._held = self._held, []
        for record in held:
            self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        assert self._file is not None
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # ---- construction ----

    @classmethod
    def create(
        cls,
        *,
        cwd: str | Path | None = None,
        role: str = "",
        model: str = "",
        directory: str | Path | None = None,
        defer: bool = False,
        **meta: Any,
    ) -> "Session":
        """Start a new session with the `meta` record that `list()` reads back.

        `defer=True` keeps that record in memory and writes no file until the first
        message. The id is real either way — the daemon registers it, a client is
        handed it — so nothing downstream has to know which kind it got.
        """
        root = sessions_dir(directory)
        session = cls(root / f"{new_id()}.jsonl", defer=defer)
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
    def list(
        cls, limit: int = 20, *, directory: str | Path | None = None
    ) -> builtins.list[dict[str, Any]]:
        """The `meta` record of each session, newest first.

        A directory listing and one line per file. No index, no SQLite: ULID filenames
        already sort by time, so the newest N are the last N names.
        """
        root = sessions_dir(directory)
        if not root.is_dir():
            return []
        summaries: builtins.list[dict[str, Any]] = []
        for path in sorted(root.glob("*.jsonl"), reverse=True)[:limit]:
            meta = _first_meta(path)
            meta.setdefault("id", path.stem)
            meta["path"] = str(path)
            meta["summary"] = _summary(path)
            summaries.append(meta)
        return summaries

    def child(self, name: str) -> "Session":
        """A separate session file for a sub-agent, linked back to this one.

        A separate *file*: interleaving a child's tool calls into the parent's
        transcript would make `messages()` produce a conversation neither agent had.
        The link is two fields in the child's `meta` — `parent` and `agent_name` —
        enough to reassemble the tree.

        From `meta()` and deliberately **not** `overrides()`: a `/model` override is a
        statement about this conversation, and inheriting it would pin
        `task(difficulty="low")` to the expensive model and make tiers decorative.

        Deferred, like any other session, so a sub-agent that fails before it says
        anything leaves no file.
        """
        return Session.create(
            cwd=self.meta().get("cwd"),
            role=self.meta().get("role", ""),
            model=self.meta().get("model", ""),
            directory=self.path.parent,
            defer=True,
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
        if self._file is None:
            # A `meta` record does not start a session; something being *said* does.
            # Held in order, so the file still opens with its meta line.
            if record["type"] == "meta":
                self._held.append(record)
                return record
            self._start()
        self._write(record)
        return record

    def set_meta(self, **fields: Any) -> dict[str, Any]:
        """Change a session setting — the model, the provider, the reasoning effort.

        On a session that has started this **appends** another `meta` record: rule 4,
        nothing is rewritten, and the transcript says when the change happened.

        On one that has not, it merges into the pending meta instead. Nothing has been
        written, so there is nothing to rewrite — and a file whose first two lines are
        both `meta` is a file that did not need the first one.
        """
        if not self.started:
            pending = next((r for r in self._records if r.get("type") == "meta"), None)
            if pending is not None:
                pending.update(fields)
                return pending
        return self.append(type="meta", **fields)

    # ---- reading ----

    def meta(self) -> dict[str, Any]:
        """The **first** `meta` record: what this session started as.

        Not merged with `overrides()`. A caller that wants the effective settings asks
        for both and says which is which — `SessionRunner.describe()` does exactly
        that — because "the model it started with" and "the model it is on now" are
        different questions and a merged dict can only answer one of them.
        """
        records = self._meta_records()
        return records[0] if records else {}

    def overrides(self) -> dict[str, Any]:
        """Every `meta` record **after** the first, merged, later wins.

        The split is the whole mechanism: `meta()` is what the session started as and
        what `list()` shows, `overrides()` is what has been changed since. The agent
        reads this before each model call, so a `/model` lands on the next call rather
        than the next session.

        Bookkeeping keys are dropped (`META_BOOKKEEPING`) so the result is settings
        only and can go straight to the gateway.
        """
        merged: dict[str, Any] = {}
        for record in self._meta_records()[1:]:
            merged.update(
                {key: value for key, value in record.items() if key not in META_BOOKKEEPING}
            )
        return merged

    def _meta_records(self) -> builtins.list[dict[str, Any]]:
        return [record for record in self._records if record.get("type") == "meta"]

    def records(self) -> builtins.list[dict[str, Any]]:
        return list(self._records)

    def tail(self, count: int = 30) -> builtins.list[dict[str, Any]]:
        """The last `count` raw records — the supervisor's input."""
        return list(self._records[-count:]) if count > 0 else []

    def messages(self) -> builtins.list[Message]:
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
        messages: builtins.list[Message] = []
        assistant_blocks: builtins.list[ContentBlock] = []
        result_blocks: builtins.list[ContentBlock] = []

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
        """Close the file if there is one. A deferred session nobody used has none, and
        closing it must leave the directory as it found it."""
        if self._file is not None and not self._file.closed:
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


SUMMARY_SCAN = 20
"""How far into a file to look for the first thing that was said. It is the second
line in every session there has ever been; the bound is there so a corrupt file cannot
turn a directory listing into a full read."""

SUMMARY_CHARS = 72


def _summary(path: Path) -> str:
    """The first thing the user said, as one short line.

    What makes a session list usable: a column of ULIDs and timestamps does not tell
    you which conversation was which, and the opening message always does.
    """
    for index, record in enumerate(read_records(path)):
        if index >= SUMMARY_SCAN:
            break
        if record.get("type") == "user":
            text = " ".join(str(record.get("content", "")).split())
            return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"
    return ""


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
    "META_BOOKKEEPING",
    "MODEL_VISIBLE",
    "Session",
    "new_id",
    "prune",
    "read_records",
    "sessions_dir",
]
