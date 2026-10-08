"""
Config tests — the file, what a patch leaves in it, and how values resolve.

`update_config` edits the user's file in place, so the failures worth guarding are the
ones that damage it: a comment lost, an environment secret written down, a patch that
leaves a file the daemon can no longer start from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from stcode.core.configs import (
    DEFAULT_CONFIG,
    Config,
    load_config,
    resolve_prompt,
    secret,
    update_config,
)


def test_the_shipped_default_config_parses() -> None:
    """It is written by hand, in a string, and nothing else would notice a typo."""
    Config.model_validate(tomlkit.parse(DEFAULT_CONFIG).unwrap())


def test_a_missing_config_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No stcode config"):
        load_config(tmp_path / "nope.toml")


def test_a_scaffolded_config_is_private(tmp_path: Path) -> None:
    """The file may come to hold a literal key, so it starts 0600."""
    path = tmp_path / "config.toml"
    load_config(path, create_if_missing=True)
    assert path.stat().st_mode & 0o777 == 0o600


# ---- secrets ----------------------------------------------------------------------


def test_a_secret_reference_resolves_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "sk-env")
    assert secret("${MY_KEY}") == "sk-env"
    assert secret("sk-literal") == "sk-literal"
    monkeypatch.delenv("MY_KEY")
    assert secret("${MY_KEY}") is None, "an unset variable must read as no key, not as its name"


def test_a_provider_falls_back_to_its_conventional_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-conventional")
    config = Config.model_validate({"model": {"providers": {"a": {"provider": "anthropic"}}}})
    assert config.model.providers["a"].key() == "sk-conventional"


def test_an_environment_secret_never_reaches_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure the old save had: a value resolved from the environment was dumped
    back into the file, and the secret was on disk from then on."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    path = tmp_path / "config.toml"
    load_config(path, create_if_missing=True)
    update_config(path, {"agent.reasoning_effort": "high"})
    assert "sk-from-env" not in path.read_text()
    assert "${OPENAI_API_KEY}" in path.read_text()


# ---- patching the file ------------------------------------------------------------


def test_a_patch_keeps_comments_and_untouched_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# mine\n[agent]\nmax_turns = 7  # why seven\n')
    update_config(path, {"agent.approval_mode": "plan"})
    text = path.read_text()
    assert "# mine" in text and "# why seven" in text
    assert load_config(path).agent.max_turns == 7
    assert load_config(path).agent.approval_mode == "plan"


def test_a_patch_that_would_not_load_is_refused_before_writing(tmp_path: Path) -> None:
    """A daemon must never write the file it will fail to start from next time."""
    path = tmp_path / "config.toml"
    load_config(path, create_if_missing=True)
    before = path.read_text()
    with pytest.raises(ValueError):
        update_config(path, {"model.default": "nobody"})
    assert path.read_text() == before


def test_a_none_removes_a_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model.providers.openai]\nprovider = "openai"\nmodel = "x"\n')
    update_config(path, {"model.providers.openai.model": None})
    assert load_config(path).model.providers["openai"].model == ""


def test_overrides_apply_to_this_load_and_never_to_the_file(tmp_path: Path) -> None:
    """The failure: `--transport tcp` once, and every later start binds TCP."""
    path = tmp_path / "config.toml"
    load_config(path, create_if_missing=True)
    live = load_config(path, overrides={"daemon.transport": "tcp", "agent.approval_mode": "plan"})
    assert live.daemon.transport == "tcp"
    assert live.agent.approval_mode == "plan"
    assert load_config(path).daemon.transport == "unix"
    assert "tcp" not in path.read_text()


# ---- routing ----------------------------------------------------------------------


def test_with_no_routing_every_tier_uses_the_default_entry() -> None:
    config = Config.model_validate(
        {"model": {"providers": {"work": {"provider": "openai", "model": "gpt-x"}}}}
    )
    assert config.model.route("low") == ("work", "gpt-x")
    assert config.model.route("high") == ("work", "gpt-x")


def test_two_accounts_on_one_library_are_two_entries() -> None:
    config = Config.model_validate(
        {
            "model": {
                "default": "personal",
                "providers": {
                    "work": {"provider": "openai", "api_key": "sk-work"},
                    "personal": {"provider": "openai", "api_key": "sk-me"},
                },
                "routing": {"low": {"provider": "work", "model": "mini"}},
            }
        }
    )
    assert config.model.route("high")[0] == "personal"
    assert config.model.route("low") == ("work", "mini")


def test_routing_to_an_unconfigured_entry_refuses() -> None:
    with pytest.raises(ValueError, match="no \\[model.providers"):
        Config.model_validate({"model": {"routing": {"low": {"provider": "ghost", "model": "m"}}}})


# ---- agent profiles, and the team switch ------------------------------------------


def test_a_prompt_file_is_relative_to_the_config_that_named_it(tmp_path: Path) -> None:
    """So a directory of profiles moves as a unit — which is how they get mounted."""
    profiles = tmp_path / "agents"
    profiles.mkdir()
    (profiles / "backend-dev.md").write_text("# Role: backend dev\n\nYou own the API.")
    path = profiles / "backend-dev.toml"
    path.write_text('[agent]\nprompt_file = "backend-dev.md"\n')

    assert resolve_prompt(load_config(path)) == "# Role: backend dev\n\nYou own the API."


def test_a_missing_prompt_file_refuses_loudly(tmp_path: Path) -> None:
    """A prompt file that did not mount is an agent that owns nothing, and it looks
    exactly like one that did until it starts working."""
    path = tmp_path / "backend-dev.toml"
    path.write_text('[agent]\nprompt_file = "gone.md"\n')
    with pytest.raises(FileNotFoundError, match="cannot be read"):
        resolve_prompt(load_config(path))


def test_team_mode_is_off_by_default_and_a_role_does_not_turn_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two facts, not one. A role says which agent this is; team mode says a shared
    volume is mounted — and a profile copied to a laptop used to mean both."""
    monkeypatch.delenv("STCODE_TEAM", raising=False)
    assert Config().team.enabled is False
    assert Config.model_validate({"team": {"role": "backend-dev"}}).team.enabled is False
    monkeypatch.setenv("STCODE_TEAM", "1")
    assert Config().team.enabled is True


def test_the_client_limit_is_unset_by_default() -> None:
    """Unset means "1 in a container, unlimited on the host", which `Daemon` resolves.
    A number here would have to be wrong on one of the two."""
    assert Config().daemon.max_clients is None


def test_tracing_is_off_unless_asked() -> None:
    """A session nobody is watching should not pay for a tracer."""
    assert Config().trace.enabled is False
