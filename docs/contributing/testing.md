# Testing

```bash
uv run pytest              # everything, no network
uv run pytest -m live      # the handful that call a real provider
```

## Layout

```
tests/
  conftest.py              the loop fixtures and the workspace fixture
  fakes.py                 the three test doubles
  fixtures/workspace/      a real small project, mounted as the agent's cwd
  core/                    one file per module, testing its public surface
  integration/             the flows the documentation describes
```

`tests/` is on the path (`pythonpath` in `pytest.ini`), so any test module can
`from fakes import ...` regardless of how deep it sits.

## Unit or integration

| | `tests/core/` | `tests/integration/` |
| --- | --- | --- |
| Tests | one class, through its own surface | a flow a page in these docs describes |
| Example | `Session.messages()` folds tool calls correctly | an agent built from a config file sends what the workspace holds |
| Fakes | the layer directly above the one under test | the SDK, and nothing else |

The rule for `tests/core/` is **the methods the module exposes**, not its internals. The
component contracts in [Architecture](../architecture/index.md) are that list.

## The three doubles

Pick the **lowest** one that still leaves the code under test real.

| Double | Replaces | Leaves real | Audits |
| --- | --- | --- | --- |
| `FakeProvider` | the vendor SDK | `LLMGateway` | routing, retry, credentials, the request on the wire |
| `RecordingGateway` | `LLMGateway` | `Agent`, `Harness` | the turn loop: messages, tools, system prompt per call |
| `FakeAgent` | `Agent` | `SessionRunner`, `Daemon` | the protocol: what is framed and broadcast |

```python
from fakes import calls_tool, fake_provider, says

with fake_provider([calls_tool("c1", "read", path="src/util.py"), says("It adds up cents.")]) as fake:
    ...
assert fake.last.model == "fake-large"
assert "Role: backend developer" in fake.last.system
assert {"read", "grep"} <= set(fake.last.tool_names)
```

`fake_provider` registers itself in the provider registry, so the **real** gateway runs
— its credential resolution and its instance cache included. Only the SDK is gone.

## What is not faked

Files, subprocesses, sockets and skill files on disk are real.

Mocking `pathlib` would test our beliefs about `pathlib`; mocking `rg` would test our
beliefs about `rg`. The parts worth catching — an edit that matches twice, a scope check
that lets a path through, a denylist that refuses `rm -rf build/` — all live in exactly
the code a mock would replace.

## The fixture workspace

`tests/fixtures/workspace/` is a small, checked-in project: source files, an
`AGENTS.md`, a role, two skills, a config, and an MCP server. The `workspace` fixture
copies it into `tmp_path` per test, so tests can edit it without sharing state.

It exists because a search path that finds **nothing** looks, from outside, exactly like
a model ignoring what it found. Each discovery mechanism gets an assertion that what is
on disk reached the prompt.

Integration tests also run with an isolated home: `~/.agents/skills` is redirected and
`STCODE_AGENTS_DIR` / `STCODE_MCP_CONFIG` are unset, so a prompt assertion does not
depend on what the developer happens to have installed.

## Driving coroutines

No `pytest-asyncio`. Two fixtures, and they are not interchangeable:

| | Use it for |
| --- | --- |
| `run(coro)` — module-scoped loop | anything holding a subprocess or MCP connection: transports bind to the loop that created them |
| `@asynctest` — fresh loop per test | anything binding a socket: a shared loop carries one daemon's server into the next test |

## Live tests

Two tests call real providers. They are marked `live` and deselected by default
(`addopts = -m "not live"`), because a suite that goes red when a machine has no API key
is a suite everyone learns to ignore.

Run them when you want to know whether a provider's wire format still matches its
adapter:

```bash
uv run pytest -m live
```

## Smoke scripts

```bash
uv run python smoke_repl.py         # the REPL, and tool_out
uv run python smoke_mcp.py          # three MCP servers, prompt prefix unchanged
uv run python smoke_supervisor.py   # a loop caught and redirected
uv run python smoke_daemon.py       # detach/re-attach, and the autonomy guard
uv run python smoke_team.py         # two containers, one volume (needs Docker)
```

One per phase gate, run by hand against real processes and a real model. They are meant
to be **read** as much as run — each is the shortest correct example of driving its
layer.

## Type checking

```bash
uv run mypy --strict stcode/core
```

!!! warning "Not currently clean"

    `mypy --strict` reports about 38 errors, mostly from provider SDK stubs
    (`google-genai`'s optional `id`/`name` fields, `openai`'s overloads) plus a handful
    of real ones. The convention is documented and the tool is installed; making it
    pass is outstanding work, not a claim about the present.
