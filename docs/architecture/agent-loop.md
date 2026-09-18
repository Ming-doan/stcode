# The agent loop

`core/agent/agent.py`. Message in, events out. Everything else in `core/` exists to be
called from here.

```py
agent = await Agent.create(config, cwd=Path.cwd(), role="backend-dev")

await agent.push("Add rate limiting to the API")
async for event in agent.events():
    match event:
        case TextDelta():    out(event.text)
        case ToolStarted():  spinner(event.name)
        case TurnFinished(): idle(event.usage)

async for event in agent.run("one turn, to completion"): ...
text = await agent.result("…")          # the final message only — what `task` returns
```

`run()` is `push()` then `events()` until the first terminal event, and `result()` is
`run()` keeping only the text. **One machine, three doors — do not write a second loop.**

## The stop condition

The model stopped calling tools. That is all of it.

No `answer` dict, no `ready` flag, no sentinel string: models already end a turn by
talking, and a protocol layered on top of that is a protocol the model has to be taught,
gets wrong occasionally, and costs tokens to restate every turn.

`max_turns` (default 40) caps tool calls *within* one turn — the only thing between a
model that keeps grepping and an unbounded bill. Hitting it ends the turn with a loud
`AgentFailed` naming the ceiling, never a silent stop.

## One turn, step by step

```
_run_turn(text)
├── span "agent.turn <name>"                  ← tracing, if enabled
└── _turn_body(text)
    ├── drain the team mailbox, append `user` record
    └── for iteration in range(max_turns):
        ├── _stream()            → TextDelta / ReasoningDelta / ToolCallEnd / MessageStop
        │                          and one `usage` record per model call
        ├── append `assistant` + one `tool_call` record per call
        ├── no calls?  → TurnFinished. done.
        ├── _run_tools(calls)    → concurrent, capped by `max_concurrent`
        ├── _deliver()           ← the steering point (below)
        └── _supervise(n)        → a `supervisor` record every `every` iterations
```

Two orderings are load-bearing:

* **Every call gets a result before the next request.** Nothing between the stream and
  the tool loop may return early — a `tool_use` block with no matching `tool_result` is
  a 400 on the following turn. That is why a provider failure appends an `error` record
  and returns *before* any tool call is recorded.
* **Tools of one turn run concurrently, and their results are recorded in call order.**
  `asyncio.gather` with no `return_exceptions`: `Harness.invoke()` never raises for a
  tool-level failure, so an exception here is a harness bug, not news for the model.

## Steering: a push mid-turn

A message pushed while a turn is running is delivered at the **next tool-call
boundary** — after this round of results, before the next request — by `_deliver()`.

That boundary is the only legal place for it. Earlier would mean splicing a `user`
message between an assistant's `tool_use` and its `tool_result`, which providers reject;
later (the old behaviour, at the top of the next turn) meant waiting out up to forty
tool calls before the agent heard you.

The same call drains the team mailbox, for the same reason.

To actually stop the work instead, `interrupt()`: it sets the flag the loop checks
between steps *and* the cancellation event every `Runtime` watches, so a tool in flight
sees it at its next `raise_if_cancelled()` and the stream stops at its next event.

## Events

`core/agent/events.py`. Deliberately *not* a re-wrapping of the provider's stream:
`TextDelta` and `ReasoningDelta` are re-exported unchanged, because a parallel
`AgentTextDelta` would be a translation layer whose only job is staying in sync.

| Event | Means |
| --- | --- |
| `TextDelta` / `ReasoningDelta` | provider passthrough |
| `ToolStarted` / `ToolFinished` | a tool call accepted / returned (`preview`, not the payload) |
| `SupervisorNudge` | the supervisor changed the agent's direction, visibly |
| `TurnFinished` | the stop condition, with the full four-field `Usage` |
| `AgentFailed` | the ceiling, a provider error, or an interrupt. Terminal for this turn only |

`ToolFinished` carries a 240-char preview rather than the result: `daemon/protocol.py`
maps these one-to-one onto the wire, and a 4 MB payload does not belong on a socket.

### A sub-agent's events go out the side

They do **not** enter the parent's stream. `Harness.on_event` is a callback in the same
family as `on_progress` — the host supplies it, the harness carries it, a tool calls it —
and `task` forwards every event its child produces through it, tagged with the child's
name.

Out through `on_event` rather than `yield`ed into the parent's turn, because the two are
different kinds of thing. The parent's events *are* its turn: each one has a record
behind it in the parent's session. A child's events belong to the child's transcript, and
a parent that yielded them would be claiming a history it does not have — while the tool
that is producing them is still parked inside `_run_tools`, where there is nothing to
yield from.

So it stays what it is: observational. The UI renders it, the session does not record it,
and the promise that a sub-agent's tool output never enters the parent's context is
untouched.

## `attach()` — the host's one method

```py
agent = (await Agent.create(config, cwd=root)).attach(
    on_ask=ask_the_user,
    on_approval=confirm,
    on_progress=status_line,
    tools=[my_tool, make_domain_tool],      # a Tool, or a factory taking the agent
)
```

The daemon supplies three callbacks; a notebook supplies none; an application supplies
its own tools. All of it is the same three assignments plus `register` + `allow`, so it
is written once here instead of at every call site. Returns `self`, so it chains.

A factory — `Callable[[Agent], Tool]` — is the shape a tool needs when it closes over
the agent it was built for. `task` and `send_message` are exactly that shape.

## Sub-agents: `task`

`core/agent/task.py`, and it lives there rather than in `core/harness/tools/` because it
spawns an `Agent`: a tool inside the harness that did so would point the harness at its
own caller.

```py
task(prompt, name, tools=None, scope=None, difficulty="medium")
```

The child gets its own `Harness` (via `for_subagent`), its own session file linked to the
parent's by one `parent` field in `meta`, and **shares** the parent's gateway — closing
that mid-turn would kill the parent's stream.

It runs through `child.run(prompt)` rather than `child.result(prompt)` so the events can
be forwarded as they happen; the return value is still the final message and nothing
else. A watching client therefore sees the sub-agent work, and the model still only gets
the paragraph.

Four rules, each with a failure behind it:

* **`max_depth = 1`.** Sub-agents get neither `task` nor `repl`. Recursion plus no budget
  is a fork bomb.
* **A child that did not finish is a tool *error*.** `AgentFailed` from the child raises
  `ToolError` in the parent, so the result arrives with `is_error` set. Returning the
  failure as ordinary text is worse than it sounds: five scouts that never reached the
  model each hand back a sentence beginning "the sub-agent did not finish", the parent
  is given five *successful* tool results, and it reads them as findings and writes a
  confident report about five repositories nobody looked at. That is a real trajectory,
  and what caused it was an endpoint dropping five simultaneous requests
  ([providers.md](providers.md#how-many-at-once)).
* **Its tool output never enters the parent's context** — only its final message does.
  That is the entire economic argument for delegating: the sub-agent reads the twenty
  files, you get the paragraph.
* **Two writers get disjoint `scope`s.** Two agents that can write the same file is a
  design error, not a race to manage.

`task` survives in team mode. A team splits a *product* into roles with their own
checkouts and one merge boundary each; a sub-agent splits one role's *task* inside its
own checkout, creating no boundary at all. Different axes. `[agent] enable_task = false`
is the off switch if you want one.

## Lifecycle, and why `aclosing` is everywhere

```py
async with agent:                       # or: await agent.aclose()
    ...
```

`aclose()` releases the harness (MCP connections, background shells, the REPL), closes
the transcript, and closes the gateway **only if this agent built it**.

Returning early from an `async for` leaves the generator underneath suspended until the
GC gets to it — possibly after the event loop has closed, stranding a network connection
down there. One level is not enough: `GeneratorExit` into `events()` unwinds its own
frame but never awaits `_run_turn().aclose()`. So every level wraps the one under it:

```
run → events → _run_turn → _turn_body → _stream → the provider's
```

## The model is decided per call, not per agent

`_stream()` reads `session.overrides()` before every model call and passes `model`,
`provider` and `reasoning_effort` to the gateway when they are set. The override arrives
as an appended `meta` record — that is what `/model` and `/effort` do
(→ [sessions](session.md#meta-is-a-merged-view)) — so it takes effect on the next call
without restarting the agent, exactly as `set_mode` does for approvals.

Read fresh each call, not cached on the agent, for the same reason the tool definitions
are: a value cached at construction is a value that can no longer be changed by the
person watching the turn.

`difficulty` is untouched by this. A tier is a statement about *this piece of work* —
`task(difficulty="low")`, the supervisor's cheap call — and an override is a statement
about the conversation. Overrides are not inherited by sub-agents, so the two never
fight.

## Configuration that reaches here

| Key | Effect |
| --- | --- |
| `[agent] max_turns` | tool-call ceiling within one turn |
| `[agent] max_concurrent` | tools running at once |
| `[agent] difficulty` | which routing tier the main loop uses |
| `[agent] enable_task`, `max_depth` | whether sub-agents exist at all |
| `[agent] tools`, `exclude_tools` | the tool set, applied last — see [harness.md](harness.md) |
| `[supervisor] *` | see [supervisor.md](supervisor.md) |
| `[trace] *` | see [tracing.md](tracing.md) |
