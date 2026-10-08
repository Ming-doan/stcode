"""Local paths shared by client I/O services."""

import os
from pathlib import Path


def stcode_home() -> Path:
    """`~/.stcode`, or `%APPDATA%\\stcode` on Windows. Override with `STCODE_HOME`.
    The same rule the daemon uses, so both find the same socket."""
    env_path = os.environ.get("STCODE_HOME")
    if env_path:
        return Path(env_path).expanduser()
    if os.name == "nt":
        return (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "stcode"
        )
    return Path.home() / ".stcode"
