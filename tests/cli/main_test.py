"""
The command line — specifically, the one place where argv is read before click sees it.

`stcode <path>` cannot be an argument on the group: click consumes a group's arguments
before it looks for a subcommand, so declaring one there turns `stcode sessions` into
"open the directory ./sessions". These tests are the grammar that replaces it.
"""

from __future__ import annotations

from pathlib import Path

from stcode.cli.main import startup_report, workspace_argv
from stcode.core.configs import GatewayConfig

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


# ---- what --headless says about itself ------------------------------------------
#
# A container's daemon has no screen. This block is the only place it can tell you what
# it became, and each assertion below is a question somebody would otherwise answer by
# exec-ing into the container.


def _settings(tmp_path: Path) -> GatewayConfig:
    settings = GatewayConfig()
    settings.session.dir = str(tmp_path / "sessions")
    settings.defaults.model = "claude-sonnet-5"
    settings.defaults.provider = "anthropic"
    return settings


def _report(tmp_path: Path, settings: GatewayConfig, **kwargs: object) -> str:
    kwargs.setdefault("address", "0.0.0.0:7717")
    kwargs.setdefault("max_clients", 1)
    kwargs.setdefault("config_path", tmp_path / "config.toml")
    return "\n".join(startup_report(settings, **kwargs))  # type: ignore[arg-type]


def test_the_report_names_the_workspace_mode_model_and_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    text = _report(tmp_path, settings)

    assert str(Path.cwd()) in text
    assert "suggest" in text
    assert "claude-sonnet-5 (anthropic)" in text
    assert str(tmp_path / "config.toml") in text
    assert str(tmp_path / "sessions") in text


def test_the_report_says_what_this_start_up_created(tmp_path: Path) -> None:
    """The expensive failure: a mount that silently did not happen.

    A config scaffolded because `/config` was empty looks exactly like one that was
    mounted — until the daemon says which it was.
    """
    scaffolded = tmp_path / "config.toml"
    text = _report(tmp_path, _settings(tmp_path), created=[scaffolded])

    assert f"created     {scaffolded}" in text
    assert "created" not in _report(tmp_path, _settings(tmp_path))


def test_the_report_states_the_client_limit_and_the_container_verdict(
    tmp_path: Path,
) -> None:
    """Both decide something: one is rule 2 at the socket, the other is rule 5."""
    contained = _report(tmp_path, _settings(tmp_path), max_clients=1, contained=True)
    assert "1 client max" in contained and "container   yes" in contained

    host = _report(tmp_path, _settings(tmp_path), max_clients=0, contained=False)
    assert "no client limit" in host and "container   no" in host


def test_the_report_says_when_team_mode_is_off(tmp_path: Path) -> None:
    """Off is the default, so a silent report would read as on."""
    assert "team        off" in _report(tmp_path, _settings(tmp_path))

    settings = _settings(tmp_path)
    settings.team.enabled = True
    settings.team.role = "backend-dev"
    assert "team        backend-dev on /team" in _report(tmp_path, settings)
