# Embedding an agent

Everything the TUI does is available in-process. There is no separate SDK — `Agent` is
the API.

## One turn

```python
import asyncio

from stcode.core.agent import Agent, TurnFinished
from stcode.core.configs import load_config


async def main() -> None:
    config = load_config(create_if_missing=True)
    async with await Agent.create(config, cwd="~/projects/api") as agent:
        async for event in agent.run("What does src/util.py do?"):
            if isinstance(event, TurnFinished):
                print(event.text)
                print(f"{event.usage.input_tokens} in, {event.usage.output_tokens} out")


asyncio.run(main())
```

`Agent.create` is the assembly point: it builds the gateway, the harness (tools, skills,
MCP servers, the prompt), and the session, from a loaded `GatewayConfig`.

`async with` matters. `aclose()` releases MCP connections, kills background shells, and
closes the transcript. Without it a subprocess can outlive the event loop and raise at
interpreter shutdown.

## Just the answer

```python
answer = await agent.result("Summarise the retry logic in src/")
```

## A long-running agent you steer

```python
from stcode.core.agent import Agent, TextDelta, ToolStarted, TurnFinished


async def main() -> None:
    config = load_config()
    async with await Agent.create(config, cwd=".") as agent:
        await agent.push("Add rate limiting to /v1/search")

        async for event in agent.events():
            match event:
                case TextDelta():
                    print(event.text, end="", flush=True)
                case ToolStarted():
                    print(f"\n[{event.name}]")
                case TurnFinished():
                    print()
```

`events()` runs queued messages **forever**. `run()` is `push()` then `events()` until
the first terminal event.

!!! warning "One consumer"

    Whoever iterates `events()` drives the loop. Two iterators race for the same queue.
    Fan out from one, the way the daemon's `SessionRunner` does.

## The events

| Event | Meaning |
| --- | --- |
| `TextDelta` | assistant text, as it streams |
| `ReasoningDelta` | the model's deliberation — never sent back as history |
| `ToolStarted` / `ToolFinished` | a tool call, with a 240-char preview of its result |
| `SupervisorNudge` | a loop was spotted and the agent was redirected |
| `TurnFinished` | **the only stop condition** — the model replied without calling a tool |
| `AgentFailed` | the ceiling, a provider error, or an interrupt |

## Steering and interrupting

```python
await agent.push("Actually, use a token bucket.")   # lands at the next tool boundary
await agent.interrupt()                              # stops at the next checkpoint
```

A `push` arriving mid-turn is delivered after the current round of tool results and
before the next request — never spliced into the model call in flight. That is the only
place a `user` message is legal: a `tool_use` block without its matching `tool_result`
is a 400 on the following request.

## Wiring in callbacks and your own tools

```python
agent = (await Agent.create(config, cwd=".")).attach(
    on_approval=my_approver,     # async (ApprovalRequest) -> bool
    on_ask=my_question_handler,  # async (Question) -> str
    on_progress=print,           # (str) -> None | Awaitable[None]
    tools=[deploy, rollback],    # Tool objects, or factories taking the agent
)
```

`attach` returns the agent, so it chains onto `Agent.create(...)`. It is the one method
a host needs — a daemon supplies three callbacks, a notebook supplies none, an
application supplies its own tools, and all of it is the same few assignments.

## Driving the harness directly

For tools without a model in the loop:

```python
from stcode.core.harness import Harness

harness = await Harness.create(cwd=".", approval_mode="auto-edit")
result = await harness.invoke("grep", {"pattern": "retry", "path": "src"})
print(result.content)
```

Or make the runtime ambient, so tools can be called as plain functions:

```python
with harness.bind():
    print(await read("src/util.py"))
```

## Resuming

```python
from stcode.core.session import Session

session = Session.resume("01HXZ8Q2K3M4N5P6R7S8T9V0W1")
agent = await Agent.create(config, cwd=".", session=session)
```

Opening a session file means **adopting** it. The agent's history is the conversation
that happened, not an empty one appended to an old transcript.
