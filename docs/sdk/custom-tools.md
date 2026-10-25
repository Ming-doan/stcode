# Custom tools

Give an embedded agent a Python function it can call. The `@tool` decorator uses
your type hints and docstring to describe the function and its arguments.

## Create and attach a tool

This example adds a project-specific service lookup:

```python
import asyncio

from stcode.core.agent import Agent
from stcode.core.configs import load_config
from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.tools.base import ToolError, tool

SERVICES = {"api": "Handles search requests", "worker": "Processes background jobs"}


@tool(permission=ToolPermission.READ)
async def service_info(name: str) -> str:
    """Look up a service in the project's service catalog.

    Args:
        name: Service name, such as api or worker.
    """
    if name not in SERVICES:
        raise ToolError(f"Unknown service {name!r}. Choose from: {', '.join(SERVICES)}.")
    return SERVICES[name]


async def main() -> None:
    config = load_config()
    async with await Agent.create(config, cwd=".", approval_mode="plan") as agent:
        agent.attach(tools=[service_info])
        print(await agent.result("Use service_info to explain what the worker does."))


asyncio.run(main())
```

Use a specific name and explain when the tool is useful. Add an `Args:` section
for parameters. Raise `ToolError` for a problem the agent can respond to, such as
an unknown service or unavailable record.

## Choose a permission

| Permission | Appropriate for |
| --- | --- |
| `READ` | Observing local data |
| `WRITE` | Creating or changing files |
| `EXECUTE` | Running commands or arbitrary code |
| `NETWORK` | Sending requests outside the machine |
| `INTERACTIVE` | Asking the user for input |

The agent's [approval mode](../guide/approval-modes.md) determines whether a call
proceeds, asks, or is unavailable. Attach an approval callback for tools that need
review in your chosen mode; see [Python SDK](embedding.md#handle-approvals-and-questions).

## Report progress and use the workspace

A `Runtime` parameter gives the tool access to the workspace and progress reporting.
It is supplied by stcode rather than requested from the model:

```python
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime


@tool(permission=ToolPermission.READ)
async def project_file_size(
    path: str,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Return the size of a workspace file.

    Args:
        path: A path inside the current workspace.
    """
    target = runtime.context.resolve(path)
    runtime.raise_if_cancelled()
    await runtime.progress(f"Checking {path}")
    size = await asyncio.to_thread(lambda: target.stat().st_size)
    return f"{path}: {size} bytes"
```

Attach this tool with `agent.attach(tools=[project_file_size])`. Use asynchronous
I/O or `asyncio.to_thread` when a tool performs blocking work.
