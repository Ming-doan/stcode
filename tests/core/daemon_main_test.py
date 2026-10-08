"""
`stcode-daemon` — what it says about itself, and how it refuses.

A container's daemon has no screen. The startup report is the only place it can tell
you what it became, and each assertion below is a question somebody would otherwise
answer by exec-ing into the container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import stcode.core.daemon.main as daemon_main
from stcode.core.configs import Config, load_config
from stcode.core.daemon import Daemon
from stcode.core.daemon.main import main, overrides_from, parse_args, startup_report


def _config(tmp_path: Path, **sections: object) -> Config:
    config = Config.model_validate(
        {
            "session": {"dir": str(tmp_path / "sessions")},
            "model": {"providers": {"anthropic": {"provider": "anthropic", "model": "claude-sonnet-5"}}},
            **sections,
        }
    )
    config.path = tmp_path / "config.toml"
    return config


def _report(config: Config, **kwargs: object) -> str:
    return "\n".join(startup_report(Daemon(config), **kwargs))  # type: ignore[arg-type]


def test_the_report_names_the_workspace_mode_model_and_paths(tmp_path: Path) -> None:
    text = _report(_config(tmp_path))
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
    assert f"created     {scaffolded}" in _report(_config(tmp_path), created=[scaffolded])
    assert "created" not in _report(_config(tmp_path))


def test_the_report_states_the_client_limit_and_the_container_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both decide something: one is rule 2 at the socket, the other is rule 5."""
    monkeypatch.setattr(daemon_main, "in_container", lambda: True)
    contained = _report(_config(tmp_path, daemon={"max_clients": 1}))
    assert "1 client max" in contained and "container   yes" in contained

    monkeypatch.setattr(daemon_main, "in_container", lambda: False)
    host = _report(_config(tmp_path, daemon={"max_clients": 0}))
    assert "no client limit" in host and "container   no" in host


def test_the_report_says_when_team_mode_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off is the default, so a silent report would read as on."""
    monkeypatch.delenv("STCODE_TEAM", raising=False)
    assert "team        off" in _report(_config(tmp_path))
    text = _report(_config(tmp_path, team={"enabled": True, "role": "backend-dev", "shared_dir": "/team"}))
    assert "team        backend-dev on /team" in text


def test_flags_become_overrides_and_never_reach_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    args = parse_args(["--config", str(path), "--transport", "tcp", "--port", "9000", "--mode", "plan"])
    overrides = overrides_from(args)
    assert overrides == {"daemon.transport": "tcp", "daemon.port": 9000, "agent.approval_mode": "plan"}

    live = load_config(path, overrides=overrides, create_if_missing=True)
    assert (live.daemon.transport, live.daemon.port) == ("tcp", 9000)
    assert "9000" not in path.read_text()


def test_full_auto_on_the_host_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 5, as an exit code a launcher can tell apart from a crash."""
    monkeypatch.setattr("stcode.core.daemon.autonomy.in_container", lambda: False)
    code = main(["--config", str(tmp_path / "config.toml"), "--socket", str(tmp_path / "d.sock"), "--mode", "full-auto"])
    assert code == 2
    assert not (tmp_path / "d.sock").exists(), "the guard runs before the bind"
