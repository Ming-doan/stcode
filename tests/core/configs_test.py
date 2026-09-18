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
