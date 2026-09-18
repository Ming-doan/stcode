# Writing a tool

```python
from typing import Annotated

from pydantic import Field

from stcode.core.harness.approvals import ToolPermission
from stcode.core.harness.context import HarnessContext
from stcode.core.harness.tools.base import Runtime, ToolError, tool


@tool(permission=ToolPermission.EXECUTE)
async def deploy(
    service: str,
    environment: str = "staging",
    wait: Annotated[int, Field(ge=0, le=600)] = 120,
    runtime: Runtime[HarnessContext] = None,  # type: ignore[assignment]
) -> str:
    """Deploy one service and wait for it to come up.

    Deploys the current `main` of `service`. There is no rollback in this tool — if the
    deploy is bad, use `rollback`.

    Args:
        service: The service to deploy, as named in `infra/services.yaml`.
        environment: `staging` or `production`.
        wait: Seconds to wait for health checks. 0 returns immediately.
    """
    await runtime.progress(f"deploying {service} to {environment}…")
    ...
    return f"{service} is up in {environment}."
```

## Three rules

### The signature is the schema; the docstring is the description

Both are read off the function, so a tool's interface cannot drift from its code. The
`Args:` section becomes each parameter's description, and `Annotated[..., Field(...)]`
becomes its JSON Schema constraints.

!!! important "The docstring is the prompt"

    Write it for the model: imperative, concrete, no rationale. Say what it does, when
    to use it, and what it will refuse. Rationale — why this tool exists, why it works
    this way — goes in comments, where the model does not pay for it every turn.

### `Runtime` is the parameter the model cannot see

cwd, approval mode, cancellation, progress callbacks, the output store. It is injected
at call time and filtered out of the schema. A model that can see `approval_mode` is a
model that will try to set it.

```python
runtime.context.cwd                     # the session's working directory
runtime.context.resolve(path)           # scope-checked absolute path
runtime.raise_if_cancelled()            # cooperate with ++esc++
await runtime.progress("still going…")  # a line for the UI; nothing is recorded
await runtime.ask(Question(...))        # up to four options, put to the human
```

### A tool never decides its own permissions

It declares a `ToolPermission` once. [Approval modes](../guide/approval-modes.md)
decide what that means.

Under-claiming asks too often; over-claiming writes without asking. If risk lives in
the *argument* rather than the tool — `cat x.py` and `rm -rf build/` both arrive through
`bash` — narrow it per call:

```python
@tool(permission=ToolPermission.EXECUTE, permission_for=classify_command)
async def run(command: str, runtime: ...) -> str:
    ...
```

## Failing

Raise `ToolError` with something the model can act on.

```python
raise ToolError(
    f"{target} changed on disk since you read it. Read it again, then decide whether "
    "your change still applies."
)
```

A tool-level failure is a **normal outcome**, not an exception in the loop:
`Harness.invoke` never raises for one. It comes back as a `ToolResult` with
`is_error=True`, the model reads it, and the turn continues. An exception escaping
`invoke` means the harness is broken.

Error text is part of the prompt too. "Invalid argument" tells the model nothing;
"`old_string` appears 3 times — add surrounding lines until it is unique, or pass
`replace_all=True` if you mean all of them" tells it exactly what to do next.

## Options

| | |
| --- | --- |
| `permission` | `READ`, `WRITE` or `EXECUTE`. Default `READ`. |
| `timeout` | Seconds. `None` inherits the turn's remaining deadline. |
| `max_output` | Chars the model sees before elision. Default 8192. |
| `spill` | Whether an over-long result is parked in `tool_out`. Default on. |
| `permission_for` | Narrows `permission` per call, from validated arguments. |
| `name` / `description` | Overrides. Prefer renaming the function and writing the docstring. |

Turn `spill` off only when the output is worthless truncated — a progress echo, a
confirmation.

## Registering it

```python
agent.attach(tools=[deploy, rollback])
```

Or, if the tool needs to close over the agent itself, pass a factory that takes it —
the shape `task` uses.

## Testing it

```python
from stcode.core.harness import Harness

harness = await Harness.create(cwd=tmp_path, approval_mode="full-auto")
harness.registry.register(deploy)
harness.allow("deploy")

result = await harness.invoke("deploy", {"service": "api"})
assert not result.is_error
```

Registering is not advertising: `allowed` is a fixed list, so a tool nobody adds to it
stays registered and invisible.

→ [Testing](../contributing/testing.md)
