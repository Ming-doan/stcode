"""
Shared plumbing for the hand-run smoke tests.

Each `smoke_*.py` proves one phase's "done when" against real processes — a real model,
a real socket, a real subprocess, real containers. The `_test.py` files cover the same
ground against scripted stand-ins; these cover what a stand-in cannot.

Everything here is setup and printing. The checks live in the scripts.

    STCODE_SMOKE_MODEL=... uv run python smoke_repl.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from stcode.core.configs import GatewayConfig, ProviderConfig, RetryConfig, RouteConfig

load_dotenv("./.env")

WORKSPACE = Path(__file__).parent / "temp"
"""Scratch directory. Never your real `~/.stcode` — a smoke test that squats on your
live socket and writes into your real session history is one you stop running."""

MODEL = os.getenv("STCODE_SMOKE_MODEL", "ollama/qwen3.5:2b")
"""Override with `STCODE_SMOKE_MODEL`. The default is small and local: these scripts
test the harness, not the model."""

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
DIM = "\033[2m" if _COLOR else ""
BOLD = "\033[1m" if _COLOR else ""
GREEN = "\033[32m" if _COLOR else ""
RED = "\033[31m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""


def step(text: str) -> None:
    print(f"\n{BOLD}▌ {text}{RESET}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def bad(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def note(text: str) -> None:
    print(f"  {DIM}{text}{RESET}")


def verdict(phase: str, results: dict[str, bool]) -> int:
    """Print one line per claim and return the exit code."""
    step(phase)
    width = max(len(name) for name in results)
    for name, passed in results.items():
        print(f"  {name.ljust(width)}  {'PASS' if passed else 'FAIL'}")
    return 0 if all(results.values()) else 1


def build_config(*, subdir: str = "", approval_mode: str = "auto-edit") -> GatewayConfig:
    """A config pointed at `temp/`, on whatever endpoint `.env` names.

    `subdir` keeps two scripts from sharing a socket or a session directory when they
    are run one after the other.
    """
    root = WORKSPACE / subdir if subdir else WORKSPACE
    config = GatewayConfig()
    config.providers["openai"] = ProviderConfig(
        base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY")
    )
    for tier in ("low", "medium", "high"):
        config.routing[tier] = RouteConfig(provider="openai", model=MODEL)  # type: ignore[index]
    config.retry = RetryConfig(max_attempts=1)
    config.defaults.provider = "openai"
    config.defaults.model = MODEL
    config.defaults.approval_mode = approval_mode  # type: ignore[assignment]
    config.session.dir = str(root / "sessions")
    config.daemon.transport = "unix"
    config.daemon.socket = str(root / "smoke.sock")
    root.mkdir(parents=True, exist_ok=True)
    return config


__all__ = [
    "BOLD", "DIM", "GREEN", "MODEL", "RED", "RESET", "WORKSPACE",
    "bad", "build_config", "note", "ok", "step", "verdict",
]
