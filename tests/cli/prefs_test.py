"""
Preference tests — the TUI's own file, and the trust list in it.

`~/.stcode/ui.toml` is not `config.toml`: the daemon reads that one, including a daemon
in a container, and a container has no theme and trusts nothing
(→ `docs/decisions/0003-what-the-tui-owns.md`).

The trust list is the security-relevant half. What it must never do is *fail open* — an
unreadable or hand-mangled file has to read as "nothing is trusted", not as "everything
is".
"""

from __future__ import annotations

from pathlib import Path

from stcode.cli.prefs import (
    DEFAULT_SHELL_TIMEOUT,
    MAX_SHELL_TIMEOUT,
    UiPrefs,
    clamp_shell_timeout,
    load_prefs,
    save_prefs,
    ui_path,
)


def test_preferences_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ui.toml"
    saved = save_prefs(UiPrefs(theme="light", trusted=["/a", "/b"]), path)

    assert saved == path
    reloaded = load_prefs(path)
    assert reloaded.theme == "light"
    assert reloaded.trusted == ["/a", "/b"]


def test_a_missing_file_reads_as_the_defaults(tmp_path: Path) -> None:
    prefs = load_prefs(tmp_path / "nothing.toml")
    assert prefs.theme == "auto"
    assert prefs.trusted == []


def test_a_corrupt_file_does_not_stop_the_ui_and_trusts_nothing(tmp_path: Path) -> None:
    """A file someone hand-edited badly must not be the reason stcode will not start —
    and must not be the reason it runs somewhere nobody approved."""
    path = tmp_path / "ui.toml"
    path.write_text("theme = = = broken\n")
    prefs = load_prefs(path)
    assert prefs.theme == "auto"
    assert prefs.trusted == []


def test_an_unknown_theme_name_falls_back_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "ui.toml"
    path.write_text('theme = "solarized"\ntrusted = ["/a"]\n')
    prefs = load_prefs(path)
    assert prefs.theme == "auto"
    assert prefs.trusted == ["/a"]  # the rest of the file is still good


def test_trusting_a_folder_is_remembered_once(tmp_path: Path) -> None:
    prefs = UiPrefs()
    prefs.trust(tmp_path)
    prefs.trust(tmp_path)
    assert prefs.trusted == [str(tmp_path.resolve())]


def test_a_child_of_a_trusted_folder_is_trusted(tmp_path: Path) -> None:
    """Trust is about a project, and a project has subdirectories. Asking again for
    every one of them would train people to click through it."""
    prefs = UiPrefs()
    prefs.trust(tmp_path)
    assert prefs.is_trusted(tmp_path / "src" / "api")
    assert not prefs.is_trusted(tmp_path.parent / "elsewhere")


def test_trust_is_not_granted_by_a_prefix_of_the_name(tmp_path: Path) -> None:
    """`/home/me/work` must not trust `/home/me/work-other`. A string `startswith`
    would, which is why this compares path parts."""
    trusted = tmp_path / "work"
    trusted.mkdir()
    prefs = UiPrefs()
    prefs.trust(trusted)
    assert not prefs.is_trusted(tmp_path / "work-other")


def test_the_file_sits_next_to_whichever_config_is_in_force(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Pointing `STCODE_CONFIG` at a scratch directory takes the preferences with it,
    so a test — or a container — never touches the real one."""
    import os

    os.environ["STCODE_CONFIG"] = str(tmp_path / "config.toml")
    try:
        assert ui_path() == tmp_path / "ui.toml"
    finally:
        del os.environ["STCODE_CONFIG"]


# ---- the ! timeout ----------------------------------------------------------------


def test_the_shell_timeout_is_clamped_to_something_the_ui_can_honour() -> None:
    """`!` runs on the UI's event loop budget. A hand-edited hour is a frozen terminal,
    and a hand-edited zero is a command that can never finish."""
    assert clamp_shell_timeout(45) == 45
    assert clamp_shell_timeout(3600) == MAX_SHELL_TIMEOUT
    assert clamp_shell_timeout(0) == 1.0


def test_a_nonsense_shell_timeout_reads_as_the_default(tmp_path: Path) -> None:
    """Same rule as the theme: one bad key is no reason to forget the rest of the file."""
    path = tmp_path / "ui.toml"
    path.write_text('theme = "light"\nshell_timeout = "soon"\n')
    prefs = load_prefs(path)
    assert prefs.shell_timeout == DEFAULT_SHELL_TIMEOUT
    assert prefs.theme == "light"


def test_the_shell_timeout_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ui.toml"
    save_prefs(UiPrefs(shell_timeout=30.0), path)
    assert load_prefs(path).shell_timeout == 30.0
