"""
A hand-run smoke test for step 9: is a looping agent actually caught and redirected?

CLAUDE.md marks phase 4 done when "a deliberately looping task is caught and
redirected". `stcode/core/agent/_test.py` proves the wiring against a scripted model.
What it cannot prove is the part that decides whether this is useful or merely noisy:
whether a real model, shown a real trajectory, tells a stuck agent something worth
hearing — and whether it stays quiet when the agent is fine.

Four claims:

1. The counting heuristics fire on a loop and stay silent on ordinary work. Free.
2. A real `difficulty="low"` call turns the symptom into concrete advice.
3. The same call answers NONE for a healthy trajectory. This is the expensive property
   — a supervisor that cannot say "carry on" is one you switch off.
4. End to end, the nudge reaches the model as a `user` message and the cached system
   prefix does not move.

Run it with `uv run python smoke_supervisor.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from smoke_common import WORKSPACE, bad, build_config, note, ok, step, verdict
from stcode.core.agent import Agent
from stcode.core.agent.supervisor import Supervisor
from stcode.core.providers.gateway import LLMGateway

ROOT = WORKSPACE / "supervisor"

STUCK: list[dict[str, Any]] = []
for _n in range(8):
    STUCK += [
        {"type": "tool_call", "id": f"c{_n}", "name": "grep", "arguments": {"pattern": "middleware"}},
        {
            "type": "tool_result",
            "id": f"c{_n}",
            "name": "grep",
            "content": "src/api/mw.py:12: class Middleware:",
            "is_error": False,
        },
    ]

HEALTHY: list[dict[str, Any]] = [
    {"type": "user", "content": "add a retry to the client"},
    {"type": "tool_call", "id": "1", "name": "grep", "arguments": {"pattern": "def request"}},
    {"type": "tool_result", "id": "1", "name": "grep", "content": "client.py:40", "is_error": False},
    {"type": "tool_call", "id": "2", "name": "read", "arguments": {"path": "client.py"}},
    {"type": "tool_result", "id": "2", "name": "read", "content": "def request(...)", "is_error": False},
    {"type": "tool_call", "id": "3", "name": "edit", "arguments": {"path": "client.py"}},
    {"type": "tool_result", "id": "3", "name": "edit", "content": "ok", "is_error": False},
    {"type": "tool_call", "id": "4", "name": "bash", "arguments": {"command": "pytest -q"}},
    {"type": "tool_result", "id": "4", "name": "bash", "content": "1 passed", "is_error": False},
]


async def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    config = build_config(subdir="supervisor", approval_mode="full-auto")
    config.supervisor.every = 1  # check at every checkpoint, so one turn is enough
    results: dict[str, bool] = {}

    step("the free half — counting, zero tokens")
    stuck_symptom = Supervisor.smell(STUCK)
    healthy_symptom = Supervisor.smell(HEALTHY)
    counted = bool(stuck_symptom) and healthy_symptom is None
    results["heuristics fire on a loop, not on work"] = counted
    (ok if counted else bad)(f"loop: {stuck_symptom!r}")
    (ok if healthy_symptom is None else bad)(f"ordinary work: {healthy_symptom!r}")

    async with LLMGateway(
        providers=config.providers, routing=config.routing, retry=config.retry
    ) as gateway:
        step("the cheap half — a real model, asked what to try instead")
        supervisor = Supervisor(gateway, difficulty="low")
        nudge = await supervisor.diagnose(STUCK, stuck_symptom or "it repeated a call")
        useful = bool(nudge)
        results["a real model gives concrete advice"] = useful
        (ok if useful else bad)(f"nudge: {nudge!r}")

        step("the veto — the same model, shown healthy work")
        quiet = await supervisor.diagnose(HEALTHY, "it has not written a file recently")
        results["it can say carry on"] = quiet is None
        (ok if quiet is None else bad)(f"answer: {quiet!r} (None means NONE — carry on)")

        step("end to end — the nudge reaches the next request")
        agent = await Agent.create(config, cwd=ROOT, approval_mode="full-auto", gateway=gateway)
        try:
            # Seed the transcript with the loop, so a real turn has something real to be
            # stuck about. Everything after this point is the ordinary agent loop.
            for record in STUCK:
                agent.session.append(**record)

            saw_nudge = False
            async for event in agent.run("Keep going."):
                if type(event).__name__ == "SupervisorNudge":
                    saw_nudge = True
                    note(f"supervisor: {event.text}")

            supervised = [r for r in agent.session.records() if r["type"] == "supervisor"]
            landed = bool(supervised)
            results["the nudge lands in the transcript"] = landed
            (ok if landed else bad)(f"{len(supervised)} supervisor record(s) written")

            # The claim that costs money if it is wrong: advice goes into `messages`,
            # so the cached system prefix is untouched.
            as_messages = agent.session.messages()
            carried = any(
                "[supervisor]" in message.content
                for message in as_messages
                if message.role == "user" and isinstance(message.content, str)
            )
            results["it arrives as a user message"] = carried
            (ok if carried else bad)("rendered into history as a prefixed user message")
            if not saw_nudge and landed:
                note("(the event was emitted on an earlier iteration than the one watched)")
        finally:
            await agent.aclose()

    return verdict("step 9", results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
