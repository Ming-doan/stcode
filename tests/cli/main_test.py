"""
The command line — specifically, the one place where argv is read before click sees it.

`stcode <path>` cannot be an argument on the group: click consumes a group's arguments
before it looks for a subcommand, so declaring one there turns `stcode sessions` into
"open the directory ./sessions". These tests are the grammar that replaces it.
"""

from __future__ import annotations

from pathlib import Path

from stcode.cli.main import workspace_argv

COMMANDS = {"sessions", "config"}


def test_a_leading_path_becomes_the_workspace(tmp_path: Path) -> None:
    assert workspace_argv([str(tmp_path)], COMMANDS) == ["--cwd", str(tmp_path)]


def test_a_dot_is_a_path(tmp_path: Path) -> None:
    assert workspace_argv(["."], COMMANDS) == ["--cwd", "."]


def test_flags_after_the_path_are_untouched(tmp_path: Path) -> None:
    assert workspace_argv([str(tmp_path), "--mode", "plan"], COMMANDS) == [
        "--cwd",
        str(tmp_path),
        "--mode",
        "plan",
    ]


def test_a_subcommand_is_still_a_subcommand() -> None:
    """The reason this function exists rather than an argument on the group."""
    assert workspace_argv(["sessions", "-n", "5"], COMMANDS) == ["sessions", "-n", "5"]


def test_nothing_is_still_nothing() -> None:
    assert workspace_argv([], COMMANDS) == []


def test_a_leading_flag_is_left_alone() -> None:
    assert workspace_argv(["--headless"], COMMANDS) == ["--headless"]


def test_a_mistyped_command_is_not_read_as_a_directory() -> None:
    """`sesions` should get click's "No such command", not a workspace that does not
    exist — the two errors send you looking in completely different places."""
    assert workspace_argv(["sesions"], COMMANDS) == ["sesions"]
