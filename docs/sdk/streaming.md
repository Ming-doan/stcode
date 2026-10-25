# Streaming and sessions

## Stream a response

Use `agent.run()` to receive events for a task:

```python
import asyncio

from stcode.core.agent import Agent, AgentFailed, TextDelta, ToolStarted, TurnFinished
from stcode.core.configs import load_config


async def main() -> None:
    config = load_config()
    async with await Agent.create(config, cwd=".", approval_mode="plan") as agent:
        async for event in agent.run("Explain how this project's tests are organized."):
            if isinstance(event, TextDelta):
                print(event.text, end="", flush=True)
            elif isinstance(event, ToolStarted):
                print(f"\nUsing {event.name}")
            elif isinstance(event, TurnFinished):
                print()
            elif isinstance(event, AgentFailed):
                print(f"\nTask stopped: {event.message}")


asyncio.run(main())
```

| Event | Information |
| --- | --- |
| `TextDelta` | Incremental assistant text in `text` |
| `ReasoningDelta` | Reasoning text supplied by the model |
| `ToolStarted` | Tool name, ID, and arguments |
| `ToolFinished` | Tool name, ID, success flag, and result preview |
| `TurnFinished` | Final answer in `text` |
| `AgentFailed` | Failure or interruption details in `message` |

`run()` ends after `TurnFinished` or `AgentFailed`. A tool failure is reported by
`ToolFinished.ok`; the agent may continue and try another approach.

## Continue a conversation

Keep the same agent open to ask follow-up questions:

```python
async with await Agent.create(config, cwd=".", approval_mode="plan") as agent:
    print(await agent.result("Find the request validation code."))
    print(await agent.result("Which edge cases should its tests cover?"))
    print("Session:", agent.session.id)
```

Use `agent.push(text)` to send a follow-up while another coroutine consumes the
agent's events. For a continuously running application, consume `agent.events()`
and submit messages with `push()`. Keep one event consumer per agent. Use
`await agent.interrupt()` to stop active work, and close the agent when your
application is finished with it.

## Resume a saved session

Pass a saved session to `Agent.create`. Use the same workspace as the original
conversation:

```python
from stcode.core.session import Session

session = Session.resume("YOUR_SESSION_ID", directory=config.session.dir)
async with await Agent.create(config, cwd=".", session=session, approval_mode="plan") as agent:
    print(await agent.result("Summarize where we left off."))
```

List available sessions with `Session.list(directory=config.session.dir)` or the
`stcode sessions` command. A saved conversation restores its history, while
transient Python variables and running commands are not restored.
