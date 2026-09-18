"""
Session tests — the round trip, and the folding that the wire format demands.

Real files in `tmp_path`, because the whole class is file handling and a mock would
test our beliefs about `open()` rather than the append-only guarantee.

The folding in `messages()` is where the bugs will be. A tool call detached from the
assistant turn that made it, or two parallel results split into two user messages, are
both rejected by providers — and both look fine in the JSONL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stcode.core.providers.types import Message, TextBlock, ToolResultBlock, ToolUseBlock
from stcode.core.session import Session, new_id, prune, read_records


def test_ids_sort_by_creation_time() -> None:
    """Lexical order is time order, which is why `list()` needs no index."""
    ids = [new_id() for _ in range(50)]
    assert ids == sorted(ids)
    assert len({*ids}) == 50
    assert all(len(i) == 26 for i in ids)


def test_create_append_resume_round_trips(tmp_path: Path) -> None:
    session = Session.create(cwd=tmp_path, role="backend-dev", directory=tmp_path, model="m")
    session.append(type="user", content="Add rate limiting")
    session.append(type="assistant", content="Looking at the middleware.")
    session.close()

    resumed = Session.resume(session.id, directory=tmp_path)
    assert resumed.id == session.id
    assert resumed.meta()["role"] == "backend-dev"
    assert resumed.meta()["cwd"] == str(tmp_path)
    assert [r["type"] for r in resumed.records()] == ["meta", "user", "assistant"]

    # Resuming appends rather than replacing.
    resumed.append(type="user", content="and a test")
    resumed.close()
    assert len(list(read_records(resumed.path))) == 4


def test_append_stamps_a_timestamp_and_demands_a_type(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    record = session.append(type="user", content="hi")
    assert record["ts"].endswith("+00:00") or "T" in record["ts"]
    with pytest.raises(ValueError, match="needs a `type`"):
        session.append(content="no type")
    session.close()


def test_resume_of_a_missing_session_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No session"):
        Session.resume("NOPE", directory=tmp_path)


def test_list_returns_metadata_newest_first(tmp_path: Path) -> None:
    made = [Session.create(cwd=tmp_path, role=f"r{n}", directory=tmp_path) for n in range(3)]
    for session in made:
        session.close()

    listed = Session.list(directory=tmp_path)
    assert [entry["id"] for entry in listed] == [s.id for s in reversed(made)]
    assert listed[0]["role"] == "r2"
    assert Path(listed[0]["path"]).is_file()
    assert len(Session.list(limit=2, directory=tmp_path)) == 2


def test_list_of_an_absent_directory_is_empty(tmp_path: Path) -> None:
    assert Session.list(directory=tmp_path / "nothing-here") == []


# ---- messages() -----------------------------------------------------------------


def test_a_flat_exchange_folds_into_plain_messages(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="hello")
    session.append(type="assistant", content="hi")
    assert session.messages() == [
        Message(role="user", content="hello"),
        Message(role="assistant", content=[TextBlock(text="hi")]),
    ]
    session.close()


def test_assistant_text_and_its_tool_calls_become_one_message(tmp_path: Path) -> None:
    """Providers reject a tool call detached from the assistant turn that made it."""
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="find the middleware")
    session.append(type="assistant", content="Let me grep.")
    session.append(type="tool_call", id="c1", name="grep", arguments={"pattern": "mw"})
    session.append(type="tool_call", id="c2", name="ls", arguments={"path": "src"})
    session.append(type="tool_result", id="c1", content="src/mw.py:12", is_error=False)
    session.append(type="tool_result", id="c2", content="src/", is_error=False)
    session.append(type="assistant", content="Found it.")

    messages = session.messages()
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]

    call_turn = messages[1].content
    assert isinstance(call_turn, list) and len(call_turn) == 3
    assert isinstance(call_turn[0], TextBlock)
    assert [b.id for b in call_turn[1:]] == ["c1", "c2"]  # type: ignore[union-attr]

    # Parallel results are answered together, in one user message.
    results = messages[2].content
    assert isinstance(results, list) and len(results) == 2
    assert all(isinstance(b, ToolResultBlock) for b in results)
    session.close()


def test_a_tool_call_with_no_preceding_text_still_lands_on_the_assistant(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="go")
    session.append(type="assistant", content="")
    session.append(type="tool_call", id="c1", name="ls", arguments={})
    messages = session.messages()
    assert messages[-1].role == "assistant"
    assert messages[-1].content == [ToolUseBlock(id="c1", name="ls", input={})]
    session.close()


def test_usage_and_meta_never_reach_the_model(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="hi")
    session.append(type="usage", model="m", difficulty="high", input_tokens=12, output_tokens=3)
    session.append(type="error", message="a provider blew up")
    assert session.messages() == [Message(role="user", content="hi")]
    # ...but they are still in the trajectory, which is the point of keeping them.
    assert [r["type"] for r in session.records()] == ["meta", "user", "usage", "error"]
    session.close()


def test_supervisor_and_inbox_are_labelled_user_messages(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    session.append(type="supervisor", content="You have grepped 'mw' 4x. Read mw.py.")
    session.append(
        type="inbox", **{"from": "ba"}, subject="spec v2", refs=["/team/knowledge/spec.md"]
    )
    first, second = session.messages()
    assert first.role == "user" and first.content.startswith("[supervisor]")  # type: ignore[union-attr]
    assert "[message from ba] spec v2" in second.content
    assert "/team/knowledge/spec.md" in second.content
    session.close()


# ---- durability and housekeeping -------------------------------------------------


def test_a_truncated_last_line_does_not_make_the_session_unreadable(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="hi")
    session.close()
    with session.path.open("a") as handle:
        handle.write('{"type": "assistant", "content": "half a rec')

    resumed = Session.resume(session.id, directory=tmp_path)
    assert [r["type"] for r in resumed.records()] == ["meta", "user"]
    resumed.close()


def test_child_sessions_are_separate_files_linked_by_meta(tmp_path: Path) -> None:
    """Rule 4: no branching. A sub-agent gets its own file, not a fork of the parent."""
    parent = Session.create(cwd=tmp_path, role="main", directory=tmp_path)
    parent.append(type="user", content="do the thing")
    child = parent.child("scout")

    assert child.path != parent.path
    assert child.meta()["parent"] == parent.id
    assert child.meta()["agent_name"] == "scout"
    assert child.messages() == []  # a child starts clean, not with the parent's history
    parent.close()
    child.close()


def test_prune_keeps_the_newest(tmp_path: Path) -> None:
    made = [Session.create(directory=tmp_path) for _ in range(5)]
    for session in made:
        session.close()
    removed = prune(keep=2, directory=tmp_path)
    assert len(removed) == 3
    assert {entry["id"] for entry in Session.list(directory=tmp_path)} == {made[3].id, made[4].id}


def test_opening_an_existing_file_adopts_its_history(tmp_path: Path) -> None:
    """A Session that appends to a file it has not read describes a shorter conversation
    than the file does, and the next run reads as a continuation of one the model never
    saw. The file is the truth."""
    first = Session.create(directory=tmp_path)
    first.append(type="user", content="one")
    first.append(type="assistant", content="done")
    first.close()

    reopened = Session(first.path)
    assert len(reopened.records()) == 3
    assert [m.role for m in reopened.messages()] == ["user", "assistant"]

    reopened.append(type="user", content="two")
    assert [m.role for m in reopened.messages()] == ["user", "assistant", "user"]
    reopened.close()

    # ...and what the object holds still matches what the file holds.
    assert len(list(read_records(first.path))) == len(reopened.records())


def test_opening_a_path_that_does_not_exist_yet_starts_empty(tmp_path: Path) -> None:
    session = Session(tmp_path / "nested" / "fresh.jsonl")
    assert session.records() == [] and session.path.is_file()
    session.close()


# ---- the file appears on the first message ----------------------------------------


def test_a_deferred_session_writes_no_file_until_the_first_append(tmp_path: Path) -> None:
    """An id with nothing behind it. `/clear` is a new session, and a UI that was opened
    and closed again must not leave a transcript to list."""
    session = Session.create(cwd=tmp_path, directory=tmp_path, defer=True, model="m")
    assert session.id and not session.started
    assert not session.path.exists()
    # Readable before it exists: the meta is held in memory, not invented later.
    assert session.meta()["cwd"] == str(tmp_path)

    session.append(type="user", content="hi")
    assert session.started and session.path.is_file()
    records = list(read_records(session.path))
    assert [r["type"] for r in records] == ["meta", "user"]
    assert records[0]["model"] == "m"
    session.close()


def test_a_deferred_session_nobody_used_leaves_nothing_behind(tmp_path: Path) -> None:
    for _ in range(5):
        Session.create(directory=tmp_path, defer=True).close()
    assert list(tmp_path.glob("*.jsonl")) == []
    assert Session.list(directory=tmp_path) == []


def test_an_eager_session_still_writes_its_meta_immediately(tmp_path: Path) -> None:
    """The default is unchanged: `Session.create()` is a file on disk."""
    session = Session.create(directory=tmp_path)
    assert session.started and session.path.is_file()
    session.close()


# ---- meta is a merged view -------------------------------------------------------


def test_a_later_meta_record_overrides_an_earlier_one_without_rewriting(
    tmp_path: Path,
) -> None:
    """How `/model` reaches a running session without breaking rule 4."""
    session = Session.create(cwd=tmp_path, directory=tmp_path, model="claude-sonnet-5")
    session.append(type="user", content="why is this flaky?")
    session.append(type="meta", model="claude-opus-5", reasoning_effort="high")

    # `meta()` is still what the session *started* as — what `stcode sessions` lists.
    assert session.meta()["model"] == "claude-sonnet-5"
    assert session.overrides() == {"model": "claude-opus-5", "reasoning_effort": "high"}
    # Nothing was rewritten: both records are in the file, in order.
    assert [r["type"] for r in read_records(session.path)] == ["meta", "user", "meta"]
    session.close()


def test_the_last_override_wins_and_bookkeeping_is_not_an_override(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path, model="a")
    session.append(type="user", content="go")
    session.append(type="meta", model="b")
    session.append(type="meta", model="c", provider="anthropic")
    assert session.overrides() == {"model": "c", "provider": "anthropic"}
    # `ts` and `type` are not settings, and neither is the id.
    assert not {"ts", "type", "id"} & set(session.overrides())
    session.close()


def test_an_override_never_becomes_a_message(tmp_path: Path) -> None:
    """`meta` is not `MODEL_VISIBLE`. A model told "model=claude-opus-5" in its own
    history is being charged to read something it cannot act on."""
    session = Session.create(directory=tmp_path)
    session.append(type="user", content="go")
    session.append(type="meta", model="claude-opus-5")
    assert [m.role for m in session.messages()] == ["user"]
    session.close()


def test_setting_meta_before_the_first_message_leaves_one_meta_record(
    tmp_path: Path,
) -> None:
    """Nothing is rewritten because nothing was written. Picking a model in `/model`
    before typing must not create the file `defer` exists to avoid."""
    session = Session.create(directory=tmp_path, defer=True, model="a")
    session.set_meta(model="b", reasoning_effort="low")
    assert not session.path.exists()

    session.append(type="user", content="hi")
    records = list(read_records(session.path))
    assert [r["type"] for r in records] == ["meta", "user"]
    assert records[0]["model"] == "b" and records[0]["reasoning_effort"] == "low"
    # Folded into the first record — and still an override, because that is what it is.
    # `overrides()` is what the agent reads before each model call, so a setting that
    # landed here instead of after would otherwise apply to no call at all: `/effort`
    # chosen one message too early would silently do nothing.
    assert session.overrides() == {"model": "b", "reasoning_effort": "low"}
    session.close()


def test_setting_meta_on_a_started_session_appends(tmp_path: Path) -> None:
    session = Session.create(directory=tmp_path, model="a")
    session.append(type="user", content="hi")
    session.set_meta(model="b")
    assert session.meta()["model"] == "a"
    assert session.overrides() == {"model": "b"}
    session.close()


def test_a_child_does_not_inherit_its_parents_overrides(tmp_path: Path) -> None:
    """`task(difficulty="low")` must still route to the cheap tier. An override that
    silently upgraded every scout to the expensive model makes tiers decorative."""
    parent = Session.create(cwd=tmp_path, directory=tmp_path, model="claude-sonnet-5")
    parent.append(type="user", content="go")
    parent.set_meta(model="claude-opus-5", reasoning_effort="max")

    child = parent.child("scout")
    assert child.meta()["model"] == "claude-sonnet-5"
    assert child.overrides() == {}
    parent.close()
    child.close()


def test_a_child_is_deferred_too(tmp_path: Path) -> None:
    """A sub-agent that dies before its first append leaves no file."""
    parent = Session.create(cwd=tmp_path, directory=tmp_path)
    child = parent.child("scout")
    assert not child.started and not child.path.exists()
    assert child.meta()["parent"] == parent.id and child.meta()["agent_name"] == "scout"
    child.close()
    parent.close()
    assert [entry["id"] for entry in Session.list(directory=tmp_path)] == [parent.id]


def test_the_listing_carries_the_first_thing_that_was_said(tmp_path: Path) -> None:
    """A column of ULIDs and timestamps does not tell you which conversation was
    which. The opening message does, and it is always the second line."""
    session = Session.create(cwd=tmp_path, directory=tmp_path)
    session.append(type="user", content="Add rate limiting\nto the search API")
    session.append(type="assistant", content="ok")
    session.close()

    row = Session.list(directory=tmp_path)[0]
    assert row["summary"] == "Add rate limiting to the search API"


def test_a_session_with_nothing_said_summarises_as_nothing(tmp_path: Path) -> None:
    session = Session.create(cwd=tmp_path, directory=tmp_path)
    session.close()
    assert Session.list(directory=tmp_path)[0]["summary"] == ""
