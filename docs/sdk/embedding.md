# Python SDK

Use stcode from Python to run coding tasks in a script, notebook, or application.
Install the package in your application's environment:

```bash
pip install stcode
```

Set up a provider using the [configuration guide](../getting-started/configuration.md).
The examples below use that same configuration and your exported API credentials.

## Run a task

```python
import asyncio

from stcode.core.agent import Agent
from stcode.core.configs import load_config


async def main() -> None:
    config = load_config(create_if_missing=True)
    async with await Agent.create(config, cwd=".", approval_mode="plan") as agent:
        answer = await agent.result("Explain this project and suggest a first test to add.")
        print(answer)


asyncio.run(main())
```

`cwd` selects the workspace. `plan` lets this example inspect files without making
changes. The asynchronous context manager closes the agent's resources when the
block exits.

`result()` returns the final response as text. To distinguish completion from a
failed or interrupted turn, consume `run()` events as shown in
[Streaming and sessions](streaming.md).

## Handle approvals and questions

Attach callbacks when your application needs a person to approve edits or command
execution. This complete terminal example uses the `suggest` mode:

```python
import asyncio

from stcode.core.agent import Agent, AgentFailed, TurnFinished
from stcode.core.configs import load_config
from stcode.core.harness.tools.base import ApprovalRequest, Question


async def approve(request: ApprovalRequest) -> bool:
    print(request.tool_name, request.arguments)
    answer = await asyncio.to_thread(input, "Approve? [y/N] ")
    return answer.strip().lower() == "y"


async def ask(question: Question) -> str:
    print(question.question)
    for option in question.options:
        print(f"- {option}")
    return await asyncio.to_thread(input, "> ")


async def main() -> None:
    config = load_config()
    async with await Agent.create(config, cwd=".", approval_mode="suggest") as agent:
        agent.attach(on_approval=approve, on_ask=ask, on_progress=print)
        async for event in agent.run("Add a test for the retry behavior."):
            if isinstance(event, TurnFinished):
                print(event.text)
            elif isinstance(event, AgentFailed):
                print(f"Task stopped: {event.message}")


asyncio.run(main())
```

In a web application, these callbacks can await a reply from your own UI.
An approval-based mode does not grant permission when no approval handler is
attached.

## Add your own tools

Pass decorated tools to `agent.attach(tools=[...])`.
See [Custom tools](custom-tools.md) for a runnable example.

## Tracing

The SDK uses the same [tracing configuration](../guide/tracing.md) as the terminal.
If you enable tracing in a short-lived script, flush pending exports before the
process exits:

```python
from stcode.core.common import trace

# At the end of your async main(), after closing the agents:
await asyncio.to_thread(trace.shutdown)
```
