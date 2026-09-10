"""
A hand-run smoke test for step 7: is the REPL real, and is `tool_out` real?

CLAUDE.md marks step 7 done when "`repl` runs, and variables survive across turns".
That is two claims, and one more matters just as much:

1. **A real model reaches for `repl` and gets an answer** — a real subprocess, driven
   through the whole `Agent` loop.
2. **The namespace survives between turns.** Turn one sets a variable; turn two, a
   separate model call with a separate context, reads it back.
3. **`tool_out` holds what was elided.** Rule 1 says eliding is lossless *provided the
   model can reach the rest*. This reads a file too big to fit, then slices the
   remainder out of `tool_out` — the bug in EXPECTED.md 4.1, shown fixed.

`stcode/core/repl/_test.py` covers the mechanics. This covers the model actually using
them. Run it with `uv run python smoke_repl.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from smoke_common import DIM, RESET, WORKSPACE, bad, build_config, note, ok, step, verdict
from stcode.core.agent import Agent
from stcode.core.agent.events import AgentFailed, TurnFinished

ROOT = WORKSPACE / "repl"
BIG_FILE = ROOT / "big.txt"
BIG_LINES = 4000


async def run_turn(agent: Agent, prompt: str) -> str:
    """One turn, printing what the agent does on the way."""
    note(f"> {prompt}")
    reply = ""
    async for event in agent.run(prompt):
        kind = getattr(event, "type", "")
        if kind == "tool_started":
            print(f"  {DIM}● {event.name}({_short(event.arguments)}){RESET}")
        elif kind == "tool_finished":
            print(f"  {DIM}  → {'ok' if event.ok else 'error'}: {_short(event.preview)}{RESET}")
        elif isinstance(event, TurnFinished):
            reply = event.text
        elif isinstance(event, AgentFailed):
            bad(event.message)
    return reply


def _short(value: object, limit: int = 90) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    BIG_FILE.write_text("\n".join(f"line {n}: " + "x" * 40 for n in range(BIG_LINES)))

    config = build_config(subdir="repl", approval_mode="full-auto")
    agent = await Agent.create(config, cwd=ROOT, approval_mode="full-auto")

    results: dict[str, bool] = {}
    try:
        step("a real model, a real subprocess")
        await run_turn(
            agent,
            "Use the repl tool to run exactly this Python, nothing else: "
            "`smoke_value = 6 * 7` then `print(smoke_value)`.",
        )
        used_repl = any(
            record.get("name") == "repl"
            for record in agent.session.records()
            if record.get("type") == "tool_call"
        )
        results["model reaches for repl"] = used_repl
        (ok if used_repl else bad)("the model called `repl`")

        step("the namespace survives the turn boundary")
        # Asked directly of the backend, not of the model: the claim under test is that
        # the subprocess kept the variable, not that a 2b model can be talked into
        # proving it.
        backend = agent.harness.context.repl
        assert backend is not None
        survived = False
        if used_repl:
            check = await backend.execute("print('smoke_value' in dir())")
            survived = check.stdout.strip() == "True"
        results["variables survive turns"] = survived
        (ok if survived else bad)(f"`smoke_value` still in the namespace: {survived}")

        step("tool_out holds what elide cut")
        # An explicit `limit`: `read` returns 2000 lines by default, and the claim under
        # test is about what elide cut from the *result*, not about read's own window.
        result = await agent.harness.invoke("read", {"path": "big.txt", "limit": BIG_LINES})
        elided = "tool_out" in result.content and result.output_id is not None
        note(result.content.splitlines()[len(result.content.splitlines()) // 2][:100])
        (ok if elided else bad)(f"the elision names tool_out[{result.output_id!r}]")

        cell = await agent.harness.invoke(
            "repl", {"code": f"print(len(tool_out[{result.output_id!r}].splitlines()))"}
        )
        seen = len(result.content.splitlines())
        recovered = int(cell.content.strip()) if cell.content.strip().isdigit() else 0
        reachable = recovered >= BIG_LINES > seen
        results["tool_out is reachable"] = elided and reachable
        (ok if reachable else bad)(
            f"the model saw {seen} lines and can reach {recovered} — nothing was lost"
        )
    finally:
        await agent.aclose()

    return verdict("step 7", results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
