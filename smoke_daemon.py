"""
A hand-run smoke test for phase 2: does the daemon actually keep its promises?

`main.py` drives `Agent` directly, which is phase 1. This drives it the way everything
will from now on — through a socket — and checks the two things CLAUDE.md §12 says
phase 2 is done when:

1. **detach / re-attach mid-run works.** A client pushes a prompt, drops while the turn
   is still going, and a second client attaches, replays the transcript, and watches the
   same turn finish. If the agent had died with the first client, there would be nothing
   to attach to.
2. **`full-auto` refuses to start on the host.** Invariant 5 is code, so this asks the
   daemon to start in `full-auto` and expects a refusal — a pass here is the refusal
   happening, not the daemon starting.

Not a pytest: `stcode/core/daemon/_test.py` covers all of this against a scripted
gateway, and this exists for what that cannot — a real model, a real socket, a real
file on disk. Run it with `uv run python smoke_daemon.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from stcode.core.configs import GatewayConfig, ProviderConfig, RetryConfig, RouteConfig
from stcode.core.daemon import AutonomyRefused, Daemon, DaemonClient

load_dotenv("./.env")

WORKSPACE = Path(__file__).parent / "temp"
MODEL = "ollama/qwen3.5:2b"
PROMPT = "List the files in the current directory, then say in one sentence what you see."

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


def build_config() -> GatewayConfig:
    """A config pointed at the local workspace and a socket beside it.

    Deliberately not the user's `~/.stcode/config.toml`: a smoke test that writes into
    your real session directory and squats on your real socket is a smoke test you stop
    running.
    """
    config = GatewayConfig()
    config.providers["openai"] = ProviderConfig(
        base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY")
    )
    config.routing["high"] = RouteConfig(provider="openai", model=MODEL)
    config.retry = RetryConfig(max_attempts=1)
    config.defaults.model = MODEL
    config.defaults.approval_mode = "auto-edit"
    config.session.dir = str(WORKSPACE / "sessions")
    config.daemon.transport = "unix"
    config.daemon.socket = str(WORKSPACE / "smoke.sock")
    return config


async def check_guard(config: GatewayConfig) -> bool:
    """Invariant 5: `full-auto` with nobody approving must not bind a socket here."""
    step("full-auto on the host")
    autonomous = config.model_copy(deep=True)
    autonomous.defaults.approval_mode = "full-auto"
    daemon = Daemon(autonomous)
    try:
        await daemon.start()
    except AutonomyRefused as refusal:
        ok(f"refused, as it must: {refusal}")
        return True
    await daemon.aclose()
    bad("the daemon started in full-auto on the host — invariant 5 is broken")
    return False


async def check_detach(config: GatewayConfig) -> bool:
    """Push, drop the client mid-turn, re-attach, and watch the same turn land."""
    async with Daemon(config) as daemon:
        step(f"daemon listening on {daemon.address}")

        first = await DaemonClient.connect(config)
        info = await first.create(cwd=WORKSPACE)
        ok(f"session {info['id']}")

        await first.push(PROMPT)
        seen = 0
        async for frame in first.events():
            seen += 1
            print(f"    {DIM}{frame['type']}{RESET}", end="\r")
            if seen >= 2:  # far enough into the turn that dropping is a real detach
                break

        step("dropping the client mid-turn")
        await first.aclose()
        await asyncio.sleep(0.2)
        runner = daemon.sessions[info["id"]]
        ok(f"agent still held, busy={runner.agent.busy}, watchers={runner.watchers}")

        step("re-attaching")
        second = await DaemonClient.connect(config)
        await second.attach(info["id"])
        history = await second.next_event(timeout=10)
        records = history.get("records", [])
        ok(f"replayed {len(records)} records ({history['type']})")

        reply, finished = "", False
        try:
            async for frame in second.events():
                if frame["type"] == "text_delta":
                    reply += frame["text"]
                    sys.stdout.write(frame["text"])
                    sys.stdout.flush()
                elif frame["type"] == "tool_started":
                    print(f"\n  {DIM}● {frame['name']} {frame.get('arguments', {})}{RESET}")
                elif frame["type"] == "tool_finished":
                    print(f"  {DIM}  → {frame['ok']}{RESET}")
                elif frame["type"] in ("turn_finished", "agent_failed"):
                    finished = True
                    print()
                    if frame["type"] == "agent_failed":
                        bad(frame["message"])
                    else:
                        usage = frame["usage"]
                        ok(f"turn finished — {usage['input_tokens']} in / {usage['output_tokens']} out")
                    break
        finally:
            await second.aclose()

        # The transcript is the proof: the work landed in the session whether or not a
        # client was watching at the time.
        kinds = [record["type"] for record in runner.agent.session.records()]
        ok(f"transcript: {len(kinds)} records — {', '.join(sorted(set(kinds)))}")
        return finished


async def main() -> int:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    config = build_config()
    guarded = await check_guard(config)
    detached = await check_detach(config)

    step("phase 2")
    print(f"  detach / re-attach mid-run: {'PASS' if detached else 'FAIL'}")
    print(f"  full-auto refused on host:  {'PASS' if guarded else 'FAIL'}")
    return 0 if (guarded and detached) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
