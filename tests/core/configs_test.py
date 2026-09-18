"""
Config tests — the file, and what survives a round trip through it.

`save_config` rewrites the whole file from the model, so anything the model holds that
TOML has no spelling for is a crash at save time, on a screen the user is in the middle
of using. That is what these check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stcode.core.configs import (
    DEFAULT_CONFIG_TOML,
    GatewayConfig,
    apply_cli_overrides,
    apply_provider_settings,
    load_config,
    save_config,
)


def test_the_shipped_default_config_parses() -> None:
    """It is written by hand, in a string, and nothing else would notice a typo."""
    import tomllib

    GatewayConfig.model_validate(tomllib.loads(DEFAULT_CONFIG_TOML))


def test_a_session_dir_survives_a_round_trip(tmp_path: Path) -> None:
    """`dir` is a `Path` in memory and a string in TOML. `tomli_w` cannot write a
    `PosixPath`, so the conversion has to happen on the way out, not by accident."""
    path = tmp_path / "config.toml"
    config = GatewayConfig.model_validate({"session": {"dir": "./.stcode/sessions"}})
    assert config.session.dir == Path("./.stcode/sessions")

    save_config(config, path)
    assert load_config(path).session.dir == Path("./.stcode/sessions")


def test_saving_keeps_the_settings_the_ui_just_wrote(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = apply_provider_settings(
        GatewayConfig(), provider="anthropic", api_key="literal", model="claude-opus-5"
    )
    save_config(config, path)
    reloaded = load_config(path)

    assert reloaded.defaults.model == "claude-opus-5"
    assert reloaded.providers["anthropic"].api_key == "literal"
    assert reloaded.routing["high"].model == "claude-opus-5"
    # The literal is why the file is 0600.
    assert path.stat().st_mode & 0o777 == 0o600


def test_tool_policy_reaches_the_config(tmp_path: Path) -> None:
    config = GatewayConfig.model_validate({"agent": {"exclude_tools": ["web_search"]}})
    assert config.agent.exclude_tools == ["web_search"]
    assert config.agent.tools == []


def test_tracing_is_off_unless_asked(tmp_path: Path) -> None:
    """A session nobody is watching should not pay for a tracer."""
    assert GatewayConfig().trace.enabled is False


def test_a_missing_config_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No stcode config"):
        load_config(tmp_path / "nope.toml")


# ---- flags are not settings -------------------------------------------------------


def test_cli_overrides_leave_the_loaded_config_alone() -> None:
    """The failure: `stcode --transport tcp` once, and every bare `stcode` afterwards
    binds TCP.

    The UI writes its config back whenever a mode or an address changes. If the flags
    were folded into that same object, the flag went to disk with it — and the docstring
    promising "never written back" was describing something that had stopped being true.
    """
    stored = GatewayConfig()
    assert stored.daemon.transport == "unix"

    live = apply_cli_overrides(stored, transport="tcp", approval_mode="full-auto")

    assert live.daemon.transport == "tcp", "the flag did not take effect for this run"
    assert stored.daemon.transport == "unix", "the flag reached the config that gets saved"
    assert stored.defaults.approval_mode != "full-auto"


def test_the_shipped_daemon_default_is_a_unix_socket() -> None:
    """`unix` and `~/.stcode/daemon.sock`, with nothing configured. A `tcp` in somebody's
    file got there from a flag or from the connect screen, not from us."""
    daemon = GatewayConfig().daemon
    assert daemon.transport == "unix"
    assert daemon.socket == "~/.stcode/daemon.sock"


# ---- what the setup screen writes -------------------------------------------------


def test_routing_models_are_applied_over_the_default_repointing() -> None:
    """The three tier fields are the last word: a tier you typed into is a tier you
    meant, even though choosing a model repoints every tier that was following it."""
    config = apply_provider_settings(
        GatewayConfig(),
        provider="openai",
        model="gpt-5",
        routing_models={"low": "gpt-5-mini", "medium": "", "high": "gpt-5"},
    )
    assert config.routing["low"].model == "gpt-5-mini"
    assert config.routing["medium"].model == "gpt-5", "a blank tier follows the model field"
    assert config.routing["low"].provider == "openai"


def test_the_concurrency_cap_survives_a_round_trip(tmp_path: Path) -> None:
    """The one knob that makes a local model usable behind parallel sub-agents."""
    path = tmp_path / "config.toml"
    config = apply_provider_settings(
        GatewayConfig(), provider="openai", model="local", max_concurrent=1
    )
    assert config.providers["openai"].max_concurrent == 1
    save_config(config, path)
    assert load_config(path).providers["openai"].max_concurrent == 1


def test_a_chosen_effort_is_remembered(tmp_path: Path) -> None:
    """`/effort` used to reach the running session and nothing else, so the choice
    quietly reverted at the next `stcode`."""
    path = tmp_path / "config.toml"
    config = apply_provider_settings(
        GatewayConfig(), provider="openai", model="m", reasoning_effort="high"
    )
    save_config(config, path)
    assert load_config(path).defaults.reasoning_effort == "high"
