"""
Integration scaffolding — a real workspace, a real config, no network.

`tests/core/` tests one class through its own surface. These tests drive the thing the
documentation describes: build an agent from a config file in a project directory and
watch what it loads, what it sends, and what it leaves behind.

`asynctest` is re-exported so a test in here can say `from conftest import asynctest`
like every other module does — this file shadows the root one by that name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from driving import asynctest

from stcode.core.harness.prompts import AGENTS_DIR_ENV
from stcode.core.harness.skills import loader as skills_loader


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Cut every search path that would otherwise reach the developer's own machine.

    Without this, `SkillRegistry.discover` finds whatever is in `~/.agents/skills` and
    the prompt these tests assert on differs per laptop. `USER_SKILLS_DIR` is a
    module-level constant computed from `Path.home()` at import time, so setting `HOME`
    is too late — the attribute itself is what has to move.

    `STCODE_AGENTS_DIR` and `STCODE_MCP_CONFIG` are unset for the same reason: an
    environment variable exported in one shell must not decide what a test discovers.
    """
    home = tmp_path / "home"
    (home / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setattr(skills_loader, "USER_SKILLS_DIR", home / ".agents" / "skills")
    monkeypatch.delenv(AGENTS_DIR_ENV, raising=False)
    monkeypatch.delenv(skills_loader.SKILLS_PATH_ENV, raising=False)
    monkeypatch.delenv("STCODE_MCP_CONFIG", raising=False)
    return home


@pytest.fixture
def config_path(workspace: Path) -> Path:
    """The fixture project's own `config.toml`, inside the copied workspace."""
    return workspace / "config.toml"


@pytest.fixture
def mcp_workspace(workspace: Path) -> Path:
    """The workspace with a launchable `.mcp.json` written into it.

    Written here rather than checked in, because it has to name *this* interpreter and
    the copy of `mcp_server.py` in *this* temporary directory. A checked-in file with
    absolute paths in it would only work on the machine that wrote it.
    """
    config = {
        "mcpServers": {
            "ledger": {"command": sys.executable, "args": [str(workspace / "mcp_server.py")]}
        }
    }
    (workspace / ".mcp.json").write_text(json.dumps(config, indent=2))
    return workspace


__all__ = ["asynctest", "isolated_home"]
