"""
Paths — where stcode's own files live on this machine.

One fact, needed on both sides of a dependency edge: `core/configs.py` loads the config
file, and `core/harness/prompts/` looks for role profiles in the same directory. Because
`configs` imports `harness`, the prompts side used to reach back with a function-level
import to dodge the cycle. A location is not a fact about either of them, so it lives
here and both import down.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_PATH_ENV = "STCODE_CONFIG"
"""Points at one config file and nothing else. What a container sets."""


def default_config_path() -> Path:
    """`~/.stcode/config.toml`, or `%APPDATA%\\stcode\\config.toml` on Windows.
    Override with `STCODE_CONFIG`."""
    env_path = os.environ.get(CONFIG_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "stcode" / "config.toml"
    return Path.home() / ".stcode" / "config.toml"


def config_exists(path: Path | None = None) -> bool:
    """Whether a config file exists — the whole first-run test."""
    return (path or default_config_path()).exists()


def config_dir() -> Path:
    """The directory the config file lives in. Also where `.env` and `agents/` are
    looked for, which is the only reason this is a function and not an expression at
    two call sites."""
    return default_config_path().parent


__all__ = ["CONFIG_PATH_ENV", "config_dir", "config_exists", "default_config_path"]
