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
