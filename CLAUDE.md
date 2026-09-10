# CLAUDE.md

Guidance for Claude Code and any agentic contributor working in this repository.

**Authority:** the architecture is decided in [`docs/EXPECTED.md`](docs/EXPECTED.md)
(Vietnamese, v2). This file is the English working spec derived from it. Where the two
disagree, `EXPECTED.md` wins and this file is stale — say so rather than guessing.

**Status legend**, used throughout. Honesty here is the point: the previous version of
this file described an architecture that was never built, and every agent that read it
coded in the wrong direction.

| Mark | Meaning |
| --- | --- |
| ✓ | Exists and works today |
| ⟳ | Exists, but changes in the named step |
| ✗ | Does not exist yet — build in the named step |

Steps 1–10 are all built, so nothing carries ✗ today. The legend stays because the next
thing anyone adds will start there.

---

## 1. What This Project Is

`stcode` is a **general agent harness, specialised for coding**. In team mode it puts one
agent per container so a set of them can work together; in solo mode a single daemon holds
however many sessions you have projects.

The unit of deployment is a **daemon**: a long-lived process holding one agent, speaking
JSONL over a socket. A TUI is just a client of it. That inversion — daemon first, UI
second — is what makes the container story real instead of aspirational.

One binary, three shapes, and nothing switches implementation between them:

| | |
| --- | --- |
| `stcode` | UI + a daemon, started here if none is listening |
| `stcode --headless` | the daemon alone — what a container runs |
| `stcode --daemonless` | the UI alone, attached to a daemon elsewhere (`/connect` to move it) |

**Two modes, one engine.** `Agent` does not know which mode it is in; the difference is
which harness is loaded and which tools are injected.

| | **Solo** | **Team** |
| --- | --- | --- |
| Deployment | one daemon on your machine | N containers, each = 1 daemon + 1 agent |
| Workspace | existing checkout at `cwd`, scope-enforced | agent runs `git clone` itself; container is the isolation |
| Roles | none | BA / frontend-dev / backend-dev / devops … |
| Coordination | `task` tool (in-process sub-agent) | shared `/team` volume + `send_message` tool |
| Client | TUI over unix socket | TUI/web over TCP; attach to any container and steer it mid-run |
| Approval | human in the loop | `full-auto`, guarded by container detection |

**Non-goals.** Not a training harness. Not a reimplementation of MCP. Not feature parity
with Claude Code. No self-modifying prompts (§4 rule 6). Simplicity beats coverage —
but *simplicity of the path*, not of the destination: anything serving the
multi-container team goal is the product, not scope creep.

---

## 2. What Makes It Different

Four things, each traceable to a measurement rather than a preference. If you are about
to remove one, read the number first.

### 2.1 MCP tools are code, not tool definitions

Every other coding agent ships MCP tool schemas in the prompt prefix on every turn. Three
mid-sized servers cost 10–30k tokens per turn, forever.

`stcode` writes them to `.stcode/mcp_servers/<server>/<tool>.py` and lets the agent
`grep` and `import` what it needs from the REPL. Anthropic measured this pattern at
**150k → 2k tokens, a 98.7% reduction**. Intermediate results stay in the REPL process
and never touch the context.

This is also the honest remnant of the RLM idea: *context as a variable*, obtained
without the kernel↔harness RPC bridge that the original design required.
→ §6.2 / §6.3 of `EXPECTED.md`, step 7–8. **Built.** Measured here on three servers and
twelve tools: **9,869 → 0 chars** of tool definitions in the prefix, every turn
(`smoke_mcp.py`). The stubs open their own connection inside the REPL, so there is no
reverse-RPC bridge either.

### 2.2 A supervisor watches the trajectory

NVIDIA AVO reached 100% on ARC-AGI-3 and attributed it to system design, not model
strength. The component they name is a supervisor that watches for **stagnation and
repeated unproductive cycles** and redirects the agent.

`stcode` already writes its trajectory — the session JSONL — so the supervisor is just a
reader. Cheap counting heuristics run first at zero token cost (same tool+args ≥3×, error
rate >50%, no file written in N turns, same file edited ≥4×); only when one fires does it
spend a `difficulty="low"` call. Its nudge is appended as a `user` message, never as a
system-prompt edit, so prompt caching survives. → step 9. **Built.** `every` counts
iterations *within* a turn, because that is where a loop actually happens. The cheap
model may answer NONE, and usually should.

### 2.3 Agent-to-agent messaging has no protocol

Containers already share a volume, so a message is a **file**: `send_message` writes JSON
into `/team/inbox/<role>/`, and the receiver drains its own directory at the start of each
turn. Shared knowledge is a directory the existing `read`/`write`/`grep` already handle.

No registry, no routing table, no service discovery, no N² socket mesh. Messages carry
`refs` (paths) rather than content — Anthropic's filesystem-output pattern, and the thing
that keeps team token cost from exploding. → step 10. **Built**, and `refs` is enforced
rather than requested: `send_message` refuses a body over 2000 chars and refuses a ref
that does not exist.

### 2.4 Daemon-first, so containerization is not a rewrite

Detach does not kill the agent. Approval and questions are request–response correlated by
`execution_id` over the same JSONL framing. Transport (`unix` / `tcp`) is configuration,
not architecture. → step 5.

### 2.5 Honest comparison

| | Claude Code | Cline / Kilo | **stcode** |
| --- | --- | --- | --- |
| Tool quality (read/edit/grep/bash) | excellent | good | ✓ comparable already |
| MCP | tool defs per turn | tool defs per turn | ✓ as code, measured at 9.9k → 0 chars/turn |
| Runs headless in a container | partial | no | ✓ core design, step 5 |
| Multi-agent team across containers | no | no | ✓ step 10 — but see §11 |
| Stagnation detection | no | no | ✓ step 9 |
| Maturity, polish, ecosystem | **far ahead** | ahead | behind, and will stay behind |

We are not competing on polish. We are betting on the four rows in the middle.

---

## 3. Architecture

```mermaid
flowchart TB
    subgraph clients["Clients"]
        tui["TUI (textual)"]
        web["Web / SDK"]
    end

    subgraph container["One container = one agent = one role"]
        daemon["<b>Daemon</b> ✓<br/>socket · JSONL · session registry"]
        agent["<b>Agent</b> ✓<br/>the turn loop"]
        sup["<b>Supervisor</b> ✓<br/>stagnation detection"]
        sess["<b>Session</b> ✓<br/>JSONL append-only"]
        harn["<b>Harness</b> ✓<br/>tools · role prompt · approvals"]
        gw["<b>LLMGateway</b> ✓<br/>difficulty routing · retry"]
        repl["<b>PyREPL</b> ✓<br/>subprocess + JSONL"]

        daemon --> agent
        agent --> sess
        agent --> harn
        agent --> gw
        sup -.->|reads| sess
        sup -.->|nudge| agent
        harn -.->|tool repl| repl
        repl -.->|import| mcpcode["mcp_servers/*.py ✓"]
    end

    subgraph vol["/team volume — mounted into every container"]
        know["knowledge/"]
        inbox["inbox/&lt;role&gt;/"]
        arte["artifacts/"]
    end

    tui -->|unix socket| daemon
    web -->|tcp| daemon
    harn -.->|read/write/grep| know
    harn -.->|send_message| inbox
    daemon -.->|watch → wake| inbox
```

### 3.1 Component contracts

Each component is one object with a small surface. If you find yourself needing a fifth
method, check whether the thing belongs somewhere else first.

**`LLMGateway`** ✓ `core/providers/gateway.py` — difficulty tier → provider+model,
credential resolution, retry before the first token only.

```py
async with LLMGateway(providers={...}, routing={...}, retry={...}) as gw:
    async for event in gw.stream(messages, system=..., tools=..., difficulty="high"): ...
```

Not a singleton — one instance per configuration; provider clients are cached inside by
`(provider, key, base_url)`. It knows nothing about sessions, tools, or the agent loop,
and must never import upward.

**`Harness`** ✓ `core/harness/harness.py` — one agent's capabilities. Per *agent*, not per
process: `for_subagent()` makes the narrowed copy.

```py
harness = await Harness.create(cwd=..., approval_mode=..., role=..., load_skills=True, load_mcp=True)
system  = harness.system_prompt()
tools   = harness.tool_definitions()
result  = await harness.invoke("read", {"path": ...}, tool_call_id=call.id)
```

`invoke()` **never raises** for a tool-level failure — bad arguments, denial, timeout, a
bug inside the tool all come back as `ToolResult(is_error=True)`. The loop's only move
after a tool call is to hand a `tool_result` back to the model; a raise takes down the turn.

**`Session`** ✓ `core/session/` — append-only JSONL. One file serving three
readers: model history, your trajectory log, and the supervisor's input.

```py
s = Session.create(cwd=..., role=...);  s = Session.resume(id);  Session.list(limit=20)
s.append(type="user", content=...)      # sync, ~20µs
s.messages()                            # -> list[Message] for the gateway
s.tail(30)                              # -> raw records for the supervisor
```

**`Agent`** ✓ `core/agent/` — the turn loop. Message in, events out.

```py
agent = await Agent.create(config, cwd=..., role=...)
await agent.push("...")
async for ev in agent.events(): ...     # TextDelta | ToolStarted | ToolFinished | TurnFinished | AgentFailed
await agent.interrupt();  await agent.aclose()
```

`run(text)` is a shortcut: `push()` then `events()` until the first `TurnFinished`.
One machine, two doors — do not write a second loop.

**`PyREPL`** ✓ `core/repl/` — a `python -u` subprocess speaking JSONL on stdin/stdout.
Persistent namespace, top-level `await` via `compile(..., PyCF_ALLOW_TOP_LEVEL_AWAIT)`,
SIGINT to interrupt, and an `inject` message that pushes `tool_out` across.

```py
repl = PyREPL(cwd=root)                    # lazy: no subprocess until the first cell
result = await repl.execute(code, timeout=120, on_stream=...)
await repl.inject({output_id: payload})    # what `elide` cut, made reachable
```

Two things that only showed up under a real load, both fixed and both worth keeping in
mind if you touch it: a cell that spawns a subprocess needs a real `fileno()` on the
redirected stderr, and the parent must drain that pipe continuously or a chatty child
deadlocks on a full buffer. A cell that answers its SIGINT keeps its namespace; one that
ignores it is killed and the caller is told the namespace is gone.

**`Daemon`** ✓ `core/daemon/` — **one daemon, many sessions**, addressed by id. Solo mode
runs a single daemon for every project you work on, one session per repo — not one
process per repo. In team mode a container usually holds one session, but nothing forbids
more.

```py
async with Daemon(config) as daemon:      # binds; `full-auto` refuses here (rule 5)
    await daemon.serve_forever()

async with await DaemonClient.connect(config) as client:
    await client.create(cwd=Path.cwd());  await client.push("…")
    async for frame in client.events(): ...
```

Four modules, one job each: `protocol.py` (the wire), `runner.py` (one live session and
the clients watching it), `server.py` (socket + registry), `autonomy.py` (rule 5, as
code). Approvals live in the **runner**, not the connection: the client that asked may be
gone by the time the answer comes, and a second client on the same session may answer
instead.

**`Supervisor`** ✓ `core/agent/supervisor.py` — reads the trajectory, counts first,
asks a cheap model only when a count fires.

```py
Supervisor.smell(records)                  # -> str | None, zero tokens
await supervisor.check(records)            # -> a nudge, or None
```

Four heuristics: the same call three times, over half the calls failing, ten calls with
no file written, one file rewritten four times. A positive costs one `difficulty="low"`
call, which may answer NONE — and for ordinary work it should.

**`Mailbox`** ✓ `core/team/mailbox.py` — a message is a file.

```py
mailbox = Mailbox("/team", role="ba")
mailbox.send("backend-dev", "spec v2", refs=["/team/knowledge/spec.md"])
mailbox.drain()                            # -> list[TeamMessage], moved to .read/
mailbox.pending()                          # what the daemon's watcher polls
```

It imports nothing from the harness, which is what keeps the arrow one-way: the
`send_message` *tool* lives in `core/team/tools.py` and closes over a mailbox, the same
shape `task` uses for the `Agent`.

### 3.2 Dependency direction

```
cli ──▶ core/daemon ──▶ core/agent ──▶ { core/session, core/harness, core/providers }
                                   └──▶ core/team (team mode only)
core/harness ──▶ core/repl        core/common ◀── everyone (imports nothing back)
```

One way, always. If `core/providers` needs `core/session`, something is inverted — that
exact temptation is why the gateway does not log (§4 rule 7). If `core` needs `cli`,
something display-shaped ended up in the engine; move it, don't reverse the arrow.

---

## 4. Hard Rules

Seven. Each one exists because violating it produced a specific, known failure.

1. **Tool output is elided at 8192 chars, never LLM-summarised.** Summarising loses
   information; eliding does not — *provided the full value is somewhere the model can
   actually reach*. The hint names `tool_out["..."]` when a REPL is attached to carry
   the payload across, and `NARROW_REQUEST_HINT` — "call again with a narrower
   offset/limit" — when one is not. `Runtime.outputs_reachable` is the single place that
   decides, so the promise cannot drift from the fact. Promising a variable that isn't
   there is the bug in `EXPECTED.md` §4.1, and it is fixed.
2. **One agent = one role = one checkout = one merge boundary.** If two agents need to
   write the same file, the roles are split wrong. That is a design error, not a signal
   to add locking.
3. **`max_depth = 1`.** Sub-agents get neither `task` nor `repl`. Unbounded recursion
   plus no budget is a fork bomb.
4. **Sessions are append-only JSONL.** Never rewrite history. No branch, no fork, no leaf
   pointer — `cp session.jsonl` is the branching feature.
5. **`full-auto` without an approver runs only inside a container.** The daemon refuses to
   start otherwise. There is no override flag. This is code (`_guard_autonomy`), not a
   warning in a document.
6. **The agent never edits its own harness.** No `/refine`, no prompt CRUD. Prime Agent
   shipped this and the agent promoted *cheating* into a skill (§11).
7. **Layers do not reach upward.** Most concretely: the gateway emits
   `MessageStop(usage=...)` and the *agent* writes it to the session. A gateway that
   imports `Session` has taken a dependency on its own caller.

---

## 5. Repository Layout

```
stcode/
  cli/                    what the user sees or types
    main.py            ✓  typer entrypoint — `stcode`, `--headless`, `--daemonless`,
                          `stcode sessions`, `stcode config`
    app.py             ✓  chat screen — a daemon client; find-or-start, or --daemonless
    connect.py         ✓  "which daemon?" screen, for --daemonless
    prompts.py         ✓  approval + question modals — the client half of §8 point 1
    settings.py        ✓  first-run wizard + /model page
    banner.py          ✓  ASCII wordmark
    labels.py          ✓  every user-facing string, in one place
  core/
    configs.py         ✓  location, schema, load/save, .env,
                          [agent]/[session]/[daemon]/[mcp]/[supervisor]/[team]
    common/            ✓  vocabulary shared across core/ — ToolDefinition, ToolResult
      truncate.py      ✓  elide() and the 8192 cap
    providers/         ✓  adapters + gateway
      types.py         ✓  unified Message/StreamEvent — the wire format
      base.py          ✓  BaseModelProvider contract
      anthropic_claude.py / openai_gpt.py / google_gemini.py
                       ✓  one adapter per SDK; cache_control lives in the
                          anthropic one and nowhere else
      registry.py      ✓  provider lookup + static metadata
      gateway.py       ✓  LLMGateway, with config fallback in _resolve_route
    harness/           ✓  the tools, prompts and skills an agent works with
      harness.py       ✓  facade, incl. role= and the MCP catalogue
      approvals.py     ✓  ApprovalMode, ToolPermission, mode policy
      errors.py        ✓  ToolError family
      context.py       ✓  HarnessContext — cwd, scope, reads, todos, git, repl
                          (+ written_files, later)
      registry.py      ✓  which tools exist, which an agent may see
      mcp.py           ✓  MCP servers → generates mcp_servers/*.py, or advertises them
      tools/           ✓  base.py (@tool, Runtime), schema.py, files/search/shell/repl/…
      prompts/         ✓  one prompt + mode note + sub-agent briefing
        roles/         ✓  ba.md, frontend-dev.md, backend-dev.md, devops.md — data
      skills/          ✓  SKILL.md discovery, loaded on demand
    session/           ✓  JSONL store, resume, messages()/tail()
    agent/             ✓  the turn loop, events, task tool, supervisor.py
    daemon/            ✓  socket server, protocol, registry, autonomy guard
      protocol.py      ✓  the JSONL message shapes — the only thing on the wire
      runner.py        ✓  SessionRunner: fan-out, approval correlation, set_mode
      server.py        ✓  Daemon + one connection per client
      autonomy.py      ✓  guard_autonomy / in_container — rule 5, enforced
    repl/              ✓  _worker.py subprocess + client.py — the persistent namespace
    team/              ✓  mailbox.py (no harness imports), tools.py (send_message)
Dockerfile             ✓  one image, all roles; --role / STCODE_ROLE picks one (§9.4)
smoke_*.py             ✓  one per phase gate, run by hand against real processes
docs/
  EXPECTED.md          ✓  the architecture decision (Vietnamese) — the authority
  evals.md             ✓  ~20 team-mode evaluation tasks — written, not yet run
```

No `docker-compose.yml`, no k8s manifests. The repo ships an image and an environment
contract; how you bring up N containers is yours, and orchestration opinions do not
belong in a Python package.

**Deleted in step 1, done:** `core/kernel/` (jupyter, ~750 lines), `core/kernel/store.py`
(`ToolOutStore` — a dict with a wrapper), `Harness.namespace()` (only meaningful with the
RPC bridge we are not building), `stcode/platform/` (empty, unreferenced), and the
`orchestrator`/`worker` prompt axis that went with the RLM design.

### 5.1 Where a given thing goes

| Kind of thing | Home | Example |
| --- | --- | --- |
| A word the user reads | `cli/labels.py` | `"read-only — no writes, no commands"` |
| A value the engine branches on | `core/` | `ApprovalMode`, `APPROVAL_MODES` order |
| A type more than two packages need | `core/common/` | `ToolDefinition`, `ToolResult`, `elide` |
| A fact about a provider | `core/providers/` | default model, conventional key env var |
| How a fact is *displayed* | `cli/labels.py` | `"OpenAI (also any OpenAI-compatible endpoint)"` |
| What a role owns and reports to | `harness/prompts/roles/*.md` | data, never code |
| Anything on the wire between processes | `core/daemon/protocol.py` | the JSONL message shapes |

Conditional copy lives in `labels.py` as a *function*, not as an `if` in a screen: the
decision about which wording applies is itself part of the wording.

---

## 6. Tool Set

Tools are ordinary Python functions. `@tool` derives the JSON Schema from the signature
and the description from the docstring, so a tool is described exactly once — **the
docstring is the prompt the model reads**. A parameter annotated `Runtime[T]` is hidden
from the schema and injected at call time; it carries identity, approval mode, the
cancellation flag, the output store, and the progress/approval/ask callbacks.
See `core/harness/tools/base.py`.

| Tool | Signature | Status / notes |
| --- | --- | --- |
| `read` | `read(path, offset=0, limit=None)` | ✓ line-numbered; elides over 8 KB |
| `write` | `write(path, content)` | ✓ scope-gated; requires a prior read of an existing file |
| `edit` | `edit(path, old, new)` | ✓ exact unique match; fails loudly on 0 or >1 |
| `bash` | `bash(command, timeout=120, background=False, cwd="", description="")` | ✓ **each call is a fresh process**, and the docstring says so. `permission_for` narrows a read-only command to READ, so `ls` does not prompt in `auto-edit` |
| `bash_output` | `bash_output(shell_id, kill=False)` | ✓ drains a background shell, new output only |
| `glob` | `glob(pattern, path=".")` | ✓ `fd`, falls back to `rg --files` then `pathlib` |
| `grep` | `grep(pattern, path=".", ...)` | ✓ ripgrep. Do not hand-roll |
| `ls` | `ls(path)` | ✓ `.gitignore`-aware |
| `todo_write` | `todo_write(items)` | ✓ not a real tool — a device to keep the plan in context. Keep it |
| `ask_user_question` | `ask_user_question(question, options=None)` | ✓ fails clearly with no user attached, rather than hanging |
| `skill` | `skill(name)` | ✓ loads a `SKILL.md` body on demand |
| `repl` | `repl(code, timeout=120)` | ✓ persistent namespace; top-level agents only (rule 3) |
| `web_search` | `web_search(query=None, url=None)` | ✓ needs `TAVILY_API_KEY`; says so on the first call if missing |
| `task` | `task(prompt, name, tools=None, scope=None, difficulty=...)` | ✓ sub-agent, solo mode only; lives in `core/agent/`. **Removed** when a team is joined |
| `send_message` | `send_message(to, subject, body, refs=None)` | ✓ team mode only; lives in `core/team/` |

**Named sets** (`core/harness/tools/__init__.py`): `MAIN_TOOLS` is a top-level agent's
allowance and `WORKER_TOOLS` is a sub-agent's — no `repl`, no `task`, because a sub-agent
that can spawn is a sub-agent for which `max_depth` stops bounding anything.
`READ_ONLY_TOOLS` is derived from declared permissions, not hand-listed, so it cannot
drift. Registered is still not advertised: a tool with no working backend stays out of
every set, because being offered a capability and then refused it wastes a turn.

Two tools are added from outside the harness, both as factories closing over something
`core/harness` must not import: `task` over an `Agent`, `send_message` over a `Mailbox`.
Joining a team removes `task` — in team mode the parallelism is containers, and a
sub-agent inside a role container answers a question the architecture already answered.

**Permissions are declared once and enforced elsewhere.** `@tool(permission=...)` states
the class of side effect; `requires_approval(mode, permission)` and
`is_forbidden(mode, permission)` in `approvals.py` decide what that means under the
current mode. A tool never tests the mode itself — a tool that knows about `full-auto` is
a tool that will disagree with the next one about what it means. Tools a mode forbids are
never advertised; being offered a capability and then refused it wastes a turn.

`NETWORK` is gated separately from the write/execute ordering: that ordering is about
workspace mutation, while network egress is a disclosure risk. `plan` therefore *asks*
for network rather than refusing it — refusing research in the mode that exists for
research would be backwards.

**Deferred:** notebook editing, multi-edit, image input.

---

## 7. Session Format

`~/.stcode/sessions/<id>.jsonl` (or `./.stcode/sessions/` — `[session] dir`). One append
per event, flushed. One file, three readers.

```jsonl
{"ts":"…","type":"meta","cwd":"/w","role":"backend-dev","model":"claude-opus-5"}
{"ts":"…","type":"user","content":"Add rate limiting to the API"}
{"ts":"…","type":"assistant","content":"Let me look at the middleware first."}
{"ts":"…","type":"tool_call","id":"c1","name":"grep","arguments":{"pattern":"middleware"}}
{"ts":"…","type":"tool_result","id":"c1","content":"src/api/mw.py:12: …","is_error":false}
{"ts":"…","type":"usage","model":"claude-opus-5","difficulty":"high","in":12043,"out":881}
{"ts":"…","type":"supervisor","content":"You have grepped 'middleware' 4×. Read mw.py."}
{"ts":"…","type":"inbox","from":"ba","subject":"spec v2","refs":["/team/knowledge/spec.md"]}
```

`messages()` folds records into `list[Message]`: `assistant` plus adjacent `tool_call`
records become one message carrying `ToolUseBlock`s; `tool_result` records become one
`user` message carrying `ToolResultBlock`s; `supervisor` and `inbox` become clearly
prefixed `user` messages. `meta` / `usage` / `error` are skipped — they are for you and
the supervisor, not the model.

Swapping to a live database later replaces one class; `Agent` sees the same four methods.
Do not write an abstraction for that now — a class with four methods **is** the abstraction.

---

## 8. Daemon Protocol

JSONL, one message per line, same framing on unix and TCP.

```
Client → Daemon
{"type":"create","cwd":"/w","role":"backend-dev"}   → {"type":"session","id":"01HX…"}
{"type":"attach","session":"01HX…"}                 # several clients may attach at once
{"type":"sessions"}                                 → what this daemon is holding
{"type":"push","text":"…"}
{"type":"interrupt"}
{"type":"approval","execution_id":"ab12","approved":true}
{"type":"answer","execution_id":"cd34","text":"Postgres"}

Daemon → Client
{"type":"text_delta","text":"…"}
{"type":"tool_started","id":"c1","name":"bash","arguments":{…}}
{"type":"tool_finished","id":"c1","ok":true,"preview":"…"}
{"type":"approval_request","execution_id":"ab12","tool":"bash","arguments":{…}}
{"type":"question","execution_id":"cd34","question":"…","options":[…]}
{"type":"turn_finished","usage":{"input_tokens":12043,"output_tokens":881,…}}
{"type":"agent_failed","message":"…"}
{"type":"error","message":"…"}
```

**Five things the sketch above leaves out**, all in `core/daemon/protocol.py`:

| Message | Why it exists |
| --- | --- |
| `detach` (C→D) | Stop watching without stopping the agent. The verb the whole layer is for |
| `set_mode` (C→D) | The TUI has had `/mode` since phase 0. Without this it would silently affect only *later* sessions |
| `history` (D→C) | The replay half of point 2 below. Raw session records, not `messages()` — a client wants what happened, not what the model was sent |
| `progress` (D→C) | `on_progress` from a long-running tool. Advisory; nothing is recorded |
| `agent_failed` (D→C) | A turn ending badly, which is the agent's own event. `error` is a protocol or daemon failure — different thing, different sender |

`turn_finished` carries the **full four-field `Usage`**, not the `{"in","out"}` sketch:
`cache_read_input_tokens` is the evidence for the caching claim in §10, and the one
client that could show it cannot if the wire drops it.

Every frame from the daemon carries a `session` id, because several clients may attach
to several sessions over one connection. Client messages take an optional `session` and
default to the last one that connection created or attached to.

**Three things to get right the first time:**

1. **Approval is request–response, not a one-way event.** `on_approval` is
   `async (ApprovalRequest) -> bool`. Over a socket it becomes: emit `approval_request`,
   await a `Future`, resolve on the matching `execution_id`. `ApprovalRequest` already
   carries `execution_id` and is already a pydantic model — **the harness needs no
   change**. Same mechanism for `ask_user_question`. Do not "simplify" this into a
   fire-and-forget event.
2. **Detach must not kill the agent.** That is the entire reason the daemon exists. The
   client drops, the agent keeps working, events keep landing in the session. On
   re-attach the daemon replays from the session, then joins the live stream.
3. **A `push` arriving mid-turn queues for the next turn.** It never interrupts the turn
   in flight and never splices into it. This is exactly why `Agent` separates `push()`
   from `events()` instead of offering only `run()` — it is what lets you attach to a
   working agent and redirect it without destroying what it is doing. To actually stop
   it, send `interrupt`.
4. **Transport is configuration.** `unix` for solo, `tcp` for containers, WebSocket later
   as a third adapter over the same protocol.
5. **A parked request whose last client leaves is failed, not left waiting.** A turn
   blocked on `approval_request` when the last watcher drops would wait forever on a
   `Future` nobody can resolve. `unsubscribe` fails every pending request with
   `ToolDenied` **naming the real reason** — not "the user declined", because a headless
   agent told a human refused it will act on that lie.

---

## 9. Team Mode

```
/team                                   # mounted into EVERY container
├── knowledge/                          # shared truth; plain files
│   ├── architecture.md
│   ├── api-contract.md
│   └── decisions/2026-09-03-rate-limit.md
├── inbox/
│   ├── backend-dev/01HX…-from-ba.json
│   └── frontend-dev/
└── artifacts/                          # large output: reports, diffs, logs
```

### 9.1 Who dispatches

**You do, per role.** There is no lead agent, no scheduler, no coordinator. The real flow:

1. You attach to the **BA** container and describe the work.
2. BA analyses it, writes the spec into `/team/knowledge/`, then `send_message`s
   frontend-dev and backend-dev.
3. You switch to another container to watch, and push a message mid-run if it drifts.

Every step of that already has a mechanism. BA calls `send_message` like any other role;
"BA is where you start" is a *convention* living in `roles/ba.md` — one English sentence,
not a class. If a `lead` role is ever wanted, it is one more markdown file. That is why
roles must stay data.

### 9.2 Shared knowledge and messaging

**`knowledge/` needs no new tool.** It is a directory; `read`, `write`, `grep`, `ls`,
`edit` already work on it. Add `/team/knowledge` to the write scope and state the
convention in the role prompt. The thing everyone wants to build as a "knowledge base
service" is `mkdir`.

**Convention, enforced by prompt rather than code:** write to `knowledge/` append-only
*per file* — one decision, one file at `decisions/<date>-<topic>.md`, never edit a file
another role owns. Same discipline as the session. No locks, no CRDT, no merge.

**Exactly one new tool**, `send_message`, whose docstring pushes `refs` over content:
messages carry pointers, not payloads. That single rule is what keeps team token cost
from growing with N².

### 9.3 Roles are data

`harness/prompts/roles/<role>.md` states what the role owns, whose output it reads, and
who it reports to. A new role is a new file, not a code change. Any file in that directory
is a valid role; `[team] role` selects one and `Harness.create(role=…)` loads it as a
prompt section, the same mechanism as `project_instructions`.

### 9.4 Deployment contract

The repo ships a `Dockerfile` and this contract, nothing more:

| | |
| --- | --- |
| Mounts | `/team` (shared volume), `/workspace` (where the agent clones), config read-only |
| Env | `STCODE_CONFIG`, `STCODE_SANDBOX=1` (unlocks `full-auto`, rule 5), provider keys |
| Port | `[daemon] transport="tcp"`, default 7717 |
| Role | `[team] role`, or `--role` / `STCODE_ROLE` — must match a file in `prompts/roles/` |
| Credentials | none. The origin is a bare repo on the volume (§9.5) |

### 9.5 Settled at step 10 — how work gets integrated

**A bare repository on the shared volume.** `/team/repo.git` is the origin. Each role
clones it into `/workspace`, works on its own branch, pushes, and **exactly one role
merges** — devops, by default.

Chosen over the documented leaning (SSH credential + a real remote) for two reasons:

- **No credential, no network.** The phase-5 gate — two containers, two roles, one
  feature — runs on your machine as it stands. The SSH route needs a real repository, a
  real key inside each container, and egress; and a private repo with no key degrades
  silently into "write into the shared volume", which is not integration.
- **It is still real git.** Real branches, a real merge, a real merge boundary. That is
  what the third candidate, a shared checkout, throws away — and the boundary is the
  whole point of rule 2.

Want pull requests instead? Point `[team] remote` at a URL and mount a key at
`[team] ssh_key`. The difference really is one config line and one sentence in
`roles/*.md`, exactly as predicted.

**Failure modes to design against** — these are the ones that actually show up:

| Failure | Cause | Guard |
| --- | --- | --- |
| Deadlock — two roles waiting on each other | nobody has a timeout | supervisor detects "waiting >20 min" |
| Message storm | no discipline | `refs` not content; role prompt names who to report to |
| Divergent knowledge | no owner | each `knowledge/` file has exactly one owning role |
| Cost explosion | multi-agent ≈ 15× tokens | `[team] max_agents`; per-role token ceiling; cheap tiers for support roles |
| Nobody integrates | no merge owner | exactly one role may merge |

---

## 10. Tech Stack

| Concern | Choice | Why |
| --- | --- | --- |
| REPL | plain `python -u` subprocess + JSONL | Persistent namespace, crash isolation, SIGINT, zero dependencies. Replaces jupyter |
| LLM clients | `anthropic`, `openai`, `google-genai` (async) | One adapter each; normalise the wire format there and nowhere else |
| Concurrency | `asyncio`, `anyio`, `asyncer` | `gather` for fan-out, `Semaphore` for caps |
| TUI | `textual` | Streaming render, attach/detach |
| Schemas | `pydantic` v2 | Signature → JSON Schema for free |
| CLI | `typer` | |
| Config | `tomllib` (read) + `tomli-w` (write) | Stdlib is read-only; the UI writes config back |
| Search | `ripgrep` (pip wheel), `fd` (optional) | The wheel drops `rg` next to the interpreter, so `grep` works on a fresh checkout |
| Tool schemas | `docstring-parser` | Signature + `Args:` → JSON Schema, described once |
| MCP | `mcp` (official SDK) | We adapt it; we never reimplement the protocol |
| Sandbox | Docker | Team mode; also what makes `full-auto` legal (rule 5) |
| Packaging | `uv` | |

**Removed in step 1:** `ipykernel`, `jupyter-client`. Done.

**Explicitly not used: LangChain.** Also LangGraph, CrewAI, AutoGen. The premise is
programmatic control over context and the loop; a framework that owns the loop defeats it.

**Knowledge you need that isn't a library:**

1. **SSE streaming + tool-call delta accumulation.** OpenAI streams
   `tool_calls[].function.arguments` in fragments; Anthropic uses `content_block_delta`.
   Normalise both in `core/providers/`, never above it.
2. **Prompt caching.** Keep the system prompt and tool definitions byte-stable across
   turns. Single biggest cost lever, and the reason supervisor nudges go into `messages`.
3. **Async lifecycle.** Cancellation, timeouts, graceful shutdown. Ctrl-C with three
   sub-agents in flight must leave no orphans and no half-written files.
4. **JSONL framing over sockets and pipes.** Used in three places now: daemon protocol,
   REPL worker, session file. Same discipline each time — one object per line, flush.
5. **Token counting.** `tiktoken` for OpenAI-family, Anthropic's count endpoint. Needed
   when compaction lands, not before.

---

## 11. Important Notices

**Arbitrary code execution.** The agent runs commands and Python with your privileges.
Rule 5 is the mitigation and it is enforced in `_guard_autonomy`, not by discipline.

**Multi-agent costs more, not less.** Anthropic measured multi-agent at **~15× the tokens**
of chat and single-agent at ~4×, with token volume explaining **80% of performance
variance** — meaning much of what looks like architectural cleverness is just paying more.
They also list **"most coding tasks"** as a *poor* fit for multi-agent: few parallelisable
subtasks, heavy interdependencies. Team mode pays off only when roles work on genuinely
separable things (spec / UI / API / pipeline), which is exactly why rule 2 draws the
boundary at the service, not the file.

**Latency increases.** Models do not reliably parallelise even when they can. Stream
progress and surface what each agent is doing, or a working session looks hung.

**No model is trained on this scaffold.** Expect under-use of `task` and over-use of plain
tool calls. Compensate in the `task` docstring — it must force an output format and a
scope, because a vaguely-described sub-agent is the number one cause of duplicated and
off-target work.

**Reward hacking is real and already documented.** Prime Agent, given a refinement loop,
found it could bypass Factorio's rules via RCON and **promoted cheating into a skill** —
even when told not to. The coding analogue is editing the test to make it pass. This is
why rule 6 exists. If you ever revisit it, the verifier must be outside the agent's write
scope, and any test-file edit during a "make the tests pass" task must be flagged.

**Debuggability is designed in, not retrofitted.** Agent → tool → sub-agent leaves no
natural stack trace. The session JSONL *is* the trace; keep every event in it, including
the ones the model never sees.

**Cost visibility.** Every LLM call lands in the session as a `usage` record. That is the
one place. Do not add a second logger — see rule 7.

---

## 12. Roadmap

Six phases. Each step leaves something that runs; nothing is a big-bang integration.
The full table with estimates is `EXPECTED.md` §15.

### Phase 0 — Shell ✓ done
Providers + gateway, config, CLI, chat/settings UI, harness tools, approval modes.
`stcode` runs, configures itself, streams a flat reply. **~9k lines, and no turn loop.**

### Phase 1 — The loop (steps 1–4) ✓ done
> *Done when: a two-file change completes end to end with a readable session log.*
> **Demonstrated** since step 6 — every `smoke_*.py` drives the real loop against a real
> model, and the session file is the readable log.

1. **Cleanup ✓.** `core/kernel/`, `store.py`, `namespace()`, `stcode/platform/` deleted;
   `truncate.py` → `core/common/`; elide hint fixed (rule 1); `_resolve_route` falls back;
   `ipykernel` + `jupyter-client` dropped.
2. **`core/session/` ✓.** Create → append → resume → `messages()` round-trips.
3. **`core/agent/` ✓.** The turn loop and `task`; no supervisor, no mailbox yet.
4. **Prompt caching + git context + `bash(cwd=)` ✓.** Three cache breakpoints (last tool,
   last system block, last message — the third is what makes the saving real); git state
   sampled once at startup; `bash` takes a `cwd`.

### Phase 2 — Daemon and client (steps 5–6) ✓ done
> *Done when: detach/re-attach mid-run works, and `full-auto` refuses to start on the host.*
> **Demonstrated**, not just tested: `smoke_daemon.py` runs both against a real model
> over a real socket. `core/daemon/_test.py` covers the same ground against a scripted
> gateway, including the whole approval chain from `Tool.invoke` to the wire and back.

5. **`core/daemon/` ✓.** Protocol (§8), approval correlation, autonomy guard.
6. **TUI is a client ✓.** `cli/app.py` opens a socket instead of the gateway; Esc →
   `interrupt`; approval and question modals; `--headless` / `--daemonless`.

### Phase 3 — Token economics (steps 7–8) ✓ done
> *Done when: three MCP servers are connected and the prompt prefix does not grow.*
> **Demonstrated.** `smoke_mcp.py` connects three servers offering twelve tools and
> measures the prefix in both modes: MCP tool definitions go 9,869 chars → 0.

7. **`core/repl/` ✓.** Subprocess worker, `inject` for `tool_out`, output streamed per
   line. The elision hint now names a place that really holds the value.
8. **MCP-as-code ✓.** `[mcp] expose = "code"` generates `.stcode/mcp_servers/*.py`;
   `"tools"` keeps the old behaviour for a server small enough not to care.

### Phase 4 — Autonomy (step 9) ✓ done
> *Done when: a deliberately looping task is caught and redirected.*
> **Demonstrated.** `smoke_supervisor.py` shows a real cheap model reading a real
> looping trajectory, naming the loop, and — shown healthy work — answering NONE.

9. **Supervisor ✓.** Heuristics first, cheap model second, nudge into `messages`.
   Checks every 8 iterations *within* a turn, which is where a loop actually happens.

### Phase 5 — Team (step 10) ✓ built
> *Done when: two containers, two roles, one feature shipped through `/team`.*
> **Partly demonstrated.** `smoke_team.py` brings up two real containers on one volume
> and checks delivery, the idle-agent wake, and invariant 5 from both sides. A whole
> feature shipped end to end is a longer run than a smoke test, and is what
> `docs/evals.md` is for.

10. **`core/team/` ✓**, plus the `Dockerfile` (§9.4). Mailbox, `send_message`,
    `roles/*.md`, daemon watches the inbox and wakes an idle agent.
    Git integration settled (§9.5): a bare repo on `/team`.
    **~20 evaluation tasks are written (`docs/evals.md`) and not yet run.** Until they
    are, "does team mode help or merely spend 15× the tokens" is unanswered — and §11
    says that is the live question, not a rhetorical one.

### Later
Compaction with a visible threshold, sessions in a live database, provider failover,
`@`-mention in the TUI, `written_files` tracking. **And running `docs/evals.md`**, which
is the only thing that can tell you whether phase 5 was worth building.

---

## 13. Conventions

- Python 3.12+, `uv` for everything. `uv run pytest` before claiming anything works.
- Type hints mandatory in `core/`. `mypy --strict` on it.
- Tests live beside the code as `<package>/_test.py` (see `core/harness/_test.py`,
  `core/providers/_test.py`), matched by `python_files = *_test.py test_*.py`.
- Every tool has a docstring, because **the docstring is the prompt the model sees**.
  Write it for the model: imperative, concrete, no rationale. Rationale goes in comments.
- No blocking I/O in the agent loop. If it can take 100 ms, it is `async`. The exception
  is `Session.append` — one line to an open file, deliberately sync.
- Commit messages: `area: what changed`. Keep them short.
- **When you change the architecture, update `docs/EXPECTED.md` first, then this file.**
  A spec that has drifted from the code is worse than no spec: every agent that reads it
  builds in the wrong direction, and none of them will tell you.
