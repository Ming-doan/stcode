"""
A hand-run smoke test for the agent core: build the four pieces, give the agent one
real task, and watch the event stream.

Not a pytest — `stcode/core/agent/_test.py` covers the loop against a scripted gateway,
and this exists for the thing that cannot cover: a real model, real tools, real files on
disk. Run it with `uv run python main.py`.

Three details worth copying into anything else that drives an `Agent`:

* **Build with `create()`, not the constructor.** `Harness.create` is async because it
  reads project instructions, discovers skills, connects MCP servers, and samples `git`.
  `Harness(...)` skips all four and hands back a working-but-blind harness. Same for
  `Session.create` vs `Session(...)`.
* **Close the gateway.** `Agent` only closes one it built itself — a sub-agent must not
  close its parent's mid-stream — so a gateway you construct is a gateway you close.
  `async with` is the whole fix, and skipping it leaks the httpx pool past the loop.
* **Stop on either terminal event.** `TurnFinished` is the happy one; `AgentFailed` is a
  provider error, an interrupt, or the tool-call ceiling. Watching only for the first
  leaves the loop waiting on an empty queue forever.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from stcode.core.agent import Agent
from stcode.core.harness import Harness
from stcode.core.providers import LLMGateway, ProviderConfig, RetryConfig, RouteConfig
from stcode.core.session import Session

load_dotenv("./.env")

WORKSPACE = Path(__file__).parent / "temp"
SESSIONS = WORKSPACE / "sessions"
PROMPT = "Write a Python function that calculates the factorial of a number."


# ---- display ---------------------------------------------------------------------
#
# The event stream is the product, so this renders it rather than `print(evt)`. Text and
# reasoning both arrive as deltas and have to be told apart visually — reasoning is the
# model thinking out loud and is not the answer, so it is dimmed and labelled once.

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str) -> str:
    return code if _COLOR else ""


DIM, BOLD, RESET = _c("\033[2m"), _c("\033[1m"), _c("\033[0m")
BLUE, GREEN, RED, YELLOW = _c("\033[34m"), _c("\033[32m"), _c("\033[31m"), _c("\033[33m")


class Display:
    """Renders the event stream, tracking which kind of delta is currently streaming.

    The state is the point: deltas arrive one word at a time with no boundaries in them,
    so the only way to know a block ended is that a different kind of event arrived.
    """

    def __init__(self) -> None:
        self._streaming: str | None = None

    def _end_block(self) -> None:
        if self._streaming is not None:
            sys.stdout.write(f"{RESET}\n")
            self._streaming = None

    def _delta(self, kind: str, header: str, text: str, style: str = "") -> None:
        if self._streaming != kind:
            self._end_block()
            sys.stdout.write(header)
            self._streaming = kind
        sys.stdout.write(f"{style}{text}")
        sys.stdout.flush()

    def reasoning(self, text: str) -> None:
        self._delta("reasoning", f"\n{DIM}thinking… ", text, DIM)

    def text(self, text: str) -> None:
        self._delta("text", "\n", text)

    def line(self, text: str) -> None:
        self._end_block()
        print(text)

    def tool_started(self, name: str, arguments: dict) -> None:
        self.line(f"{BLUE}●{RESET} {BOLD}{name}{RESET}{DIM} {_summarize(arguments)}{RESET}")

    def tool_finished(self, name: str, ok: bool, preview: str) -> None:
        # The name is repeated here on purpose: a turn's calls run concurrently, so the
        # ● lines all appear before any ✓ and the pairing is otherwise guesswork.
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        body = _one_line(preview) or "(no output)"
        self.line(f"  {mark} {DIM}{name} — {body}{RESET}")

    def close(self) -> None:
        self._end_block()


def _one_line(text: str, limit: int = 100) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _summarize(arguments: dict) -> str:
    """Tool arguments on one line. A `write` carries a whole file; nobody needs it here."""
    parts = [f"{key}={_one_line(str(value), 60)!r}" for key, value in arguments.items()]
    return _one_line(" ".join(parts), 110)


# ---- the agent -------------------------------------------------------------------


def build_gateway() -> LLMGateway:
    """One tier is enough here. `_resolve_route` falls back when a tier is missing, but
    an example should not lean on that — `difficulty` below names this tier outright."""
    return LLMGateway(
        providers={
            "openai": ProviderConfig(
                base_url=os.getenv("OPENAI_BASE_URL"),
                api_key=os.getenv("OPENAI_API_KEY"),
            )
        },
        routing={"low": RouteConfig(provider="openai", model="ollama/qwen3.5:2b")},
        retry=RetryConfig(max_attempts=1),
    )


async def main() -> int:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    display = Display()

    # `async with` is what keeps the httpx pool from outliving the event loop: `Agent`
    # was handed this gateway rather than building it, so it deliberately leaves it open.
    async with build_gateway() as gateway:
        harness = await Harness.create(cwd=WORKSPACE, approval_mode="full-auto")
        session = Session.create(cwd=WORKSPACE, directory=SESSIONS, model="ollama/qwen3.5:2b")

        agent = Agent(
            gateway=gateway,
            harness=harness,
            session=session,
            difficulty="low",
        )
        # A fresh session per run, so each run's transcript is its own file with its own
        # `meta` record. Pointing `Session` at one fixed path instead makes every run
        # append to the same file, which reads back as one long conversation that never
        # happened. `Session.resume(id, directory=SESSIONS)` is the way to genuinely
        # continue a previous run.
        display.line(f"{DIM}session {session.id} → {session.path}{RESET}")
        display.line(f"{BOLD}▌ {PROMPT}{RESET}")

        exit_code = 0
        try:
            async for event in agent.run(PROMPT):
                match event.type:
                    case "reasoning_delta":
                        display.reasoning(event.text)
                    case "text_delta":
                        display.text(event.text)
                    case "tool_started":
                        display.tool_started(event.name, event.arguments)
                    case "tool_finished":
                        display.tool_finished(event.name, event.ok, event.preview)
                    case "turn_finished":
                        display.close()
                        usage = event.usage
                        display.line(
                            f"{DIM}── {usage.input_tokens} in / {usage.output_tokens} out"
                            f" · {usage.cache_read_input_tokens} cached{RESET}"
                        )
                    case "agent_failed":
                        # The other terminal event. `agent.run()` stops on both; a loop
                        # that only watched for `turn_finished` would hang here.
                        display.line(f"{RED}✗ {event.message}{RESET}")
                        exit_code = 1
        finally:
            display.close()
            await agent.aclose()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
