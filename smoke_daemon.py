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
import sys

from smoke_common import BOLD, DIM, GREEN, RED, RESET, WORKSPACE, bad, build_config, ok, step, verdict
from stcode.core.configs import GatewayConfig
from stcode.core.daemon import AutonomyRefused, Daemon, DaemonClient

PROMPT = "List the files in the current directory, then say in one sentence what you see."


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
    config = build_config(subdir="daemon")
    guarded = await check_guard(config)
    detached = await check_detach(config)
    return verdict("phase 2", {
        "detach / re-attach mid-run": detached,
        "full-auto refused on host": guarded,
    })


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
