# CLAUDE.md

Guidance for Claude Code (and any agentic contributor) working in this repository.

---

## 1. What This Project Is

`stcode` is a **coding agent CLI** built on the **Recursive Language Model (RLM)** architecture
described in [Prime Intellect's RLM blog](https://www.primeintellect.ai/blog/rlm) and realized in
[Prime Agent](https://www.primeintellect.ai/blog/prime-agent).

**The core inversion vs. a normal ReAct agent:**

| Normal agent | RLM agent (this project) |
| --- | --- |
| Model calls tools directly; every tool output enters context | Model's **only** tool is a persistent Python REPL |
| Long outputs cause context rot | Tool outputs live as **Python variables**; model sees a truncated preview |
| Sub-agents are a fixed, hand-wired schema | Sub-agents are **async function calls** inside the REPL: `await agent(...)` |
| Answer is the final assistant message | Answer is written into an `answer` dict variable and committed with `ready=True` |

**One-line thesis:** context is a variable, not a transcript. The main agent orchestrates; sub-agents
absorb token-heavy work; nothing is ever summarized lossily.

**Non-goals:** we are not building a training harness, not reimplementing MCP, and not chasing
feature parity with Claude Code. Simplicity beats coverage.

---

## 2. Core Architecture

```
┌──────────────┐    writes code    ┌─────────────────────┐
│  Main Agent  │──────────────────▶│   Python REPL       │
│              │◀──── stdout ──────│   (IPython kernel)   │
│ system+user  │  (truncated 8KB)  │  • tool_out vars     │
│ prompt only  │                   │  • session context   │
└──────┬───────┘                   │  • answer dict       │
       │                           │  • harness (CRUD)    │
       │                           └──────────┬───────────┘
       │                                      │ await agent(...)
       │                                      ▼
       │                           ┌──────────────────────┐
       │                           │     Agent Pool       │
       │                           │  queue + semaphore   │
       │                           └──────────┬───────────┘
       │                                      │ spawns
       │      agent_message.send(...)         ▼
       └──────────────────────────▶┌──────────────────────┐
                                   │      Sub-Agent       │◀── has the real tools
                                   │  own kernel + ctx    │    (read/write/bash/...)
                                   └──────────┬───────────┘
                                              │
                                   ┌──────────▼───────────┐
                                   │     LLM Gateway      │
                                   │ difficulty → model   │
                                   │ retry / fallback     │
                                   └──────────────────────┘
```

### 2.1 Layers and their invariants

**Main Agent** — sees only: system prompt, user prompt, and truncated REPL stdout.
It has **no direct tools**. If you are tempted to give the main agent a tool, you are
undoing the architecture. Route it through a sub-agent instead.

**Python REPL** (`core/kernel/`) — a *persistent* IPython kernel (one per session), not
`exec()` per turn. It holds:
- `tool_out["<id>"]` — raw tool results, spilled to `.pkl` above the size threshold
- `session_ctx` — shared knowledge injected into sibling sub-agents
- `answer = {"content": "", "ready": False}` — the only channel for the final response
- `harness` — CRUD surface over prompts / memories / skills / sub-agent specs
- `agent`, `agent_message`, `compact`, `refine` — pre-imported at kernel init

**Agent Pool** (`core/agent/`) — bounded queue with an `asyncio.Semaphore`. Enforces
`max_concurrent`, `max_subagents_per_turn`, `max_depth`, and a recursive token budget.

**Sub-Agent** — a full agent session (own model, own kernel, own history) with a *narrow*
slice of parent context. Owns the real tools (`core/harness/`). Returns a short
synthesis, never raw output.

**LLM Gateway** (`core/providers/gateway.py`) — `difficulty` → model tier mapping, retry
with backoff, provider fallback chain, circuit breaker, and cost accounting.
Provider-specific streaming/tool-call formats are normalized in `core/providers/` and
nowhere else.

### 2.2 Hard rules

1. **REPL stdout is truncated at 8192 chars.** This is a forcing function, not a nicety.
   Never replace truncation with LLM summarization — summarization loses information;
   truncation does not, because the variable is still there.
2. **`agent()` returns at admission, not at completion.** It hands back a child handle
   immediately. Results arrive via `agent_message`. Blocking `agent()` kills fan-out and
   mid-flight steering.
3. **Default `max_depth = 1`, hard cap 2.** Unbounded recursion + no budget = fork bomb.
4. **Two sub-agents must never write the same file.** Assign disjoint path scopes at spawn.
5. **The base system prompt is immutable.** `/refine` may only edit the harness layer around it.
6. **Sessions are append-only JSONL.** Branch/fork/clone by moving the leaf pointer within
   the same file. Never rewrite history.
7. **Short tasks bypass RLM entirely.** See §7.

---

## 3. Repository Layout

Two top-level layers. **`cli/` is the interface, `core/` is the engine, and the
dependency runs one way: `cli` → `core`, never back.** If `core` ever needs to import
from `cli`, something display-shaped has ended up in the engine — move it, don't wire
the arrow backwards.

Present tense — what exists today is marked, the rest is the target shape. Everything
non-CLI nests under `core/` — if you're adding a top-level `stcode/` directory that
isn't `cli/`, you're probably one level too shallow; it belongs under `core/`.

```
stcode/
  cli/                 everything the user sees or types
    main.py          ✓ typer entrypoint (`stcode`, `stcode config`)
    app.py           ✓ chat screen, slash commands, status bar
    settings.py      ✓ first-run wizard + /model config page
    banner.py        ✓ ASCII wordmark
    labels.py        ✓ every user-facing string, in one place
  core/                the agent engine — no UI knowledge
    configs.py       ✓ config location, schema, load/save, .env loading
    common/          ✓ vocabulary shared across core/ subpackages, importing none of
                        them — ToolDefinition, ToolResult
    providers/       ✓ provider adapters + the LLM gateway built on them
      types.py       ✓ unified Message/StreamEvent shapes — the wire format every
                        adapter translates to/from (ToolDefinition is re-exported
                        from core/common, which has three consumers now)
      base.py        ✓ BaseModelProvider — the adapter contract (stream(), aclose())
      anthropic_claude.py / openai_gpt.py / google_gemini.py
                     ✓ one adapter per SDK; normalize that SDK's wire format here
                        and nowhere else
      registry.py    ✓ provider lookup by name + static metadata (default model,
                        conventional key env var)
      gateway.py     ✓ LLMGateway — difficulty routing, retry, credential
                        resolution (`ProviderConfig`/`RouteConfig`/`RetryConfig`
                        are its own domain vocabulary, not a fact about files)
    agent/              turn loop + state machine, agent pool (queue, semaphore,
                        depth guard, token budget), session state (JSONL store,
                        leaf pointer, tree ops, resume) — the workflow that drives
                        harness + providers + kernel each turn
    kernel/             jupyter_client wrapper, output capture, truncation, snapshot
    harness/         ✓ the tools, prompts, and skills an agent works with
      harness.py     ✓ Harness — the facade the agent loop talks to. Per *agent*, not
                        per process: for_subagent() narrows scope and tools
      approvals.py   ✓ approval-mode vocabulary + ToolPermission + the mode policy
      errors.py      ✓ ToolError family. A leaf, so context and tools can share it
      context.py     ✓ HarnessContext — cwd, write scope, read tracking, todos; the
                        `T` in Runtime[T]
      registry.py    ✓ which tools exist, and which an agent may see
      mcp.py         ✓ MCP servers from .mcp.json, adapted to the Tool interface
      tools/         ✓ base.py (@tool, Runtime), schema.py (signature → JSON Schema),
                        files/search/shell/repl/todo/web/interact/skill
      prompts/       ✓ plan + execute modes, orchestrator + worker roles
      skills/        ✓ SKILL.md discovery from ~/.agents/skills, loaded on demand
    daemon/             unix socket server, A2A routing, session registry, plus a
                        websocket channel for control over the internet
tests/
docs/
```

**Where a given thing goes**, when it isn't obvious:

| Kind of thing | Home | Example |
| --- | --- | --- |
| A word the user reads | `cli/labels.py` | `"read-only — no writes, no commands"` |
| A value the engine branches on | `core/` | `ApprovalMode`, `APPROVAL_MODES` order |
| A type more than two packages need | `core/common/` | `ToolDefinition`, `ToolResult` |
| A fact about a provider | `core/providers/` | default model, conventional key env var |
| How a fact is *displayed* | `cli/labels.py` | `"OpenAI (also any OpenAI-compatible endpoint)"` |

The split shows up most clearly in approval modes: `core/harness/approvals.py` owns the
names, their order, and the permission policy the tool layer enforces; `cli/labels.py`
owns the help text and the status-bar colours. Same for providers —
`core/providers/registry.py` knows `gpt-5.6` is OpenAI's default, `cli/labels.py`
decides that renders as `optional — e.g. gpt-5.6`.

Conditional copy lives in `labels.py` as a *function*, not as an `if` in a screen: the
decision about which wording applies is itself part of the wording.

---

## 4. CLI & TUI

`stcode` (typer) launches a textual app; `stcode config` prints the resolved config
without starting the UI.

**Config lives in one file** — `~/.stcode/config.toml`, or wherever `STCODE_CONFIG`
points. `stcode/core/configs.py` owns everything about it; nothing else touches paths
or TOML.

**Startup is a single test: does that file exist?**

- Absent → the setup screen opens over the chat screen. Provider (OpenAI by default,
  no base URL), API key, optional base URL, optional model. Skipping writes **nothing**,
  so the next run asks again — an empty config would silently turn the prompt off forever.
- Present → straight to chat, no questions. `/model` reopens the same screen.

**Credentials resolve env-first.** `api_key_env` names an environment variable,
`api_key` is a literal fallback, and the variable wins whenever it's set
(`providers.resolve_secret`, defined in `core/providers/gateway.py` — it's the LLM
Gateway's own domain vocabulary, `core/configs.py` only composes `ProviderConfig`
into the on-disk `GatewayConfig` shape). Files stcode writes are chmod 0600 because the
literal may be in them. When the user types a key into the UI we clear that provider's
`api_key_env`, so a stale exported variable can't shadow the key they just entered.
The API key field is write-only: blank means "keep what's stored", never "erase it".

**Saving from `/model` also repoints difficulty tiers** — any tier already on the chosen
provider, or still tracking the provider being replaced, follows the new model. A tier
deliberately pinned to a third provider is left alone.

**Slash commands** are handled by `cli/app.py`, not the agent — they never reach a model:

| Command | Effect |
| --- | --- |
| `/model` | open the settings screen (also `f2`) |
| `/mode [name]` | cycle approval modes, or jump to one by name/prefix (also `shift+tab`) |
| `/clear` | clear the transcript and the message history (also `ctrl+l`) |
| `/help`, `/quit` | |

**Approval modes** (least → most permissive): `plan`, `suggest`, `auto-edit`,
`full-auto`. Names, ordering, and policy in `core/harness/approvals.py`; help text and
colours in `cli/labels.py`. `full-auto` is the mode §9 says never to run
un-containerized.

They are **enforced** by the tool layer. A tool declares a `ToolPermission` once, at
definition (`@tool(permission=...)`), and `requires_approval(mode, permission)` decides
whether that class runs unattended — the tool never tests the mode itself, because a
tool that knows about `full-auto` is a tool that will disagree with the next one about
what it means. `is_forbidden` is separate from `requires_approval` on purpose: "ask
first" is a pause a human can resolve, while `plan` mode simply does not have the
capability, and the agent should be told which one it hit. Tools a mode forbids are
never advertised — being offered a tool and then refused it wastes a turn.

`NETWORK` is gated on its own line rather than wedged into the ordering: the mode
ordering is about workspace mutation, and network egress is a disclosure risk, not a
corruption risk. `plan` therefore *asks* for network access rather than refusing it —
refusing research in the mode that exists for research would be backwards.

**Warn, don't guess.** An empty `defaults.model` means "not chosen yet". The status bar
says so and sending is refused. Never silently substitute a default model — the user
paying for the call should know which one it is.

**The reply path is a placeholder.** `cli/app.py:_stream_reply` streams one flat
user/assistant exchange through `LLMGateway` — no REPL, no sub-agents, no tools. It
exists so the UI is exercisable end to end. When the RLM loop lands it replaces that
one method; nothing else on the screen changes.

**Textual notes worth not rediscovering:**

- `shift+tab` belongs to screen-level focus navigation. Reaching it needs
  `Binding(..., priority=True)`.
- `Select` wraps its value in a `SelectCurrent` carrying its own border. Squashing a
  `Select` to `height: 1` without flattening `SelectCurrent` renders an empty box.
- Keep dialog buttons outside the scrolling region, or they slide below the fold on a
  24-row terminal.
- Widget CSS is inlined as a `CSS` class attribute rather than `CSS_PATH`, so nothing
  depends on package data being present at runtime.

Testing the UI is headless: `App.run_test()` drives it with a `Pilot`, and
`app.export_screenshot()` returns SVG you can reconstruct a text grid from to eyeball
layout at a given terminal size.

---

## 5. Tool Set

Tools are **only available to sub-agents**. The main agent reaches them by delegating.

| Tool | Signature | Notes |
| --- | --- | --- |
| `read` | `read(path, offset=0, limit=None) -> str` | Result cached to a variable; prints preview only if oversized |
| `write` | `write(path, content)` | Permission-gated; must be inside the agent's path scope |
| `edit` | `edit(path, old, new)` | Exact, unique string match. Fails loudly on 0 or >1 matches |
| `bash` | `bash(cmd, timeout=120, background=False)` | Allowlist + denylist; output → variable |
| `glob` | `glob(pattern, path=".")` | Shells out to `fd` |
| `grep` | `grep(pattern, path=".", ...)` | Shells out to `ripgrep`. Do not hand-roll |
| `ls` | `ls(path)` | `.gitignore`-aware |
| `todo_write` | `todo_write(items)` | Not a real tool — a device to keep the plan in context. Keep it |
| `bash_output` | `bash_output(shell_id, kill=False)` | Drains a `background=True` shell. Returns only what is new since the last read |
| `web_search` | `web_search(query=None, url=None, ...)` | Tavily. `query` searches, `url` extracts, both crawl. Sub-agent only; output is always huge |
| `ask_user_question` | `ask_user_question(question, options=None, ...)` | Pauses the turn. Fails clearly when no user is attached, rather than hanging |
| `skill` | `skill(name)` | Loads a `SKILL.md` body on demand |
| `repl` | `repl(code, timeout=120)` | The *main* agent's only tool. Runs in the session kernel |

REPL-only primitives (main agent side):

```python
h       = await agent(prompt, difficulty="high", name="auth-expert", tools=[...], scope="src/auth/")
_       = await agent_message.send(text, receiver_role="child", receiver_name="auth-expert")
results = await gather(h1, h2, h3)
subs    = await agent.list_subagents()      # survives compaction + kernel restart
await compact.run()
await refine.run("promote the retry-on-flaky-test pattern to a skill")
```

Tools are ordinary Python functions. `@tool` derives the JSON Schema from the signature
and the description from the docstring, so a tool is described exactly once. A parameter
annotated `Runtime[T]` is hidden from the model and injected at call time — it carries
identity, the approval mode, the cancellation flag, the output store, and the progress /
approval / ask callbacks. See `core/harness/tools/base.py`.

**Deferred to later phases:** notebook editing, multi-edit, image input.

**A2A scope:** messaging is restricted to the *nuclear family* — parent, sibling, child.
This is deliberate; it prevents N² message storms across sessions.

---

## 6. Reference Flow

Task: *"Add Redis-backed rate limiting to the API endpoints, with tests."*

**Turn 1 — orient (cheap, no sub-agents)**

```python
print(bash("fd -e py -d 3 . src/ | head -50"))   # ~40 lines, under the truncation limit
```

**Turn 2 — parallel scouting (token-heavy work pushed down)**

```python
scout = await agent(
    "Read src/api/ and report: (1) HTTP framework, (2) existing middleware pattern, "
    "(3) every endpoint definition as a file:line -> endpoint table. Do not modify files.",
    difficulty="low", name="scout", tools=["read", "grep", "glob"],
)
probe = await agent(
    "Does this repo already have a Redis client? Grep pyproject/requirements and config. "
    "Answer yes/no plus location.",
    difficulty="low", name="redis-probe", tools=["read", "grep"],
)
findings = await gather(scout, probe)
print(findings[0][:2000])
```

`scout` may read 20 files and grep 300 KB. **None of those tokens reach the main agent** —
it receives roughly 800 tokens of table.

**Turn 3 — implement, with disjoint file scopes**

```python
ctx = f"Framework: FastAPI. Middleware at src/api/middleware/. No Redis client yet.\n{findings[0]}"
session_ctx["shared"] = ctx

impl = await agent(
    f"{ctx}\n\nCreate src/api/middleware/ratelimit.py: sliding-window limiter using "
    "redis.asyncio, configured by RATE_LIMIT_RPM. Only this file.",
    difficulty="high", name="impl", tools=["read", "write", "edit", "bash"],
    scope="src/api/middleware/",
)
test = await agent(
    f"{ctx}\n\nWrite tests/test_ratelimit.py with fakeredis + pytest-asyncio. "
    "Assume API: RateLimiter(redis, rpm).check(key) -> bool. Only this file.",
    difficulty="medium", name="test", tools=["read", "write", "bash"],
    scope="tests/",
)
await gather(impl, test)
```

**Turn 4 — verify and repair**

```python
out = bash("uv run pytest tests/test_ratelimit.py -x 2>&1 | tail -40")
print(out)   # FAILED: 'RateLimiter' object has no attribute 'check'

fixer = await agent(
    f"Tests fail:\n{out}\n\nRead both files, reconcile the interface, rerun pytest until green.",
    difficulty="high", name="fixer", tools=["read", "edit", "bash"],
)
await gather(fixer)
```

**Turn 5 — commit the answer**

```python
answer["content"] = """Added rate limiting:
- src/api/middleware/ratelimit.py — sliding window, Redis backend
- tests/test_ratelimit.py — 6 tests, all passing
- Requires RATE_LIMIT_RPM (default 60)
Not done: not yet wired into the app factory, see src/api/main.py:23"""
answer["ready"] = True
```

**Budget outcome:** main agent context ≈ 12k tokens. The same task in a flat ReAct loop
lands around 180k. That gap is the entire point of the architecture.

---

## 7. When *Not* To Use RLM

Prime Intellect's own ablations found the RLM scaffold **hurt** performance on math-python,
despite permitting identical behavior to a plain LLM — the scaffolding overhead made the model
reason worse. The lesson generalizes.

Bypass the RLM path and run a flat agent loop when:
- the task touches ≤ 2 files
- no tool is expected to produce > 8 KB of output
- the user asked a question rather than requested a change
- total estimated context < 30k tokens

Route this decision explicitly in `agent/router.py`. Do not let it be implicit.

---

## 8. Tech Stack

| Concern | Choice | Why |
| --- | --- | --- |
| REPL | `jupyter_client` + `ipykernel` | Persistent namespace, native async, interrupt, clean output capture |
| LLM client | `httpx` + `openai` SDK (async) | OpenAI-compatible endpoints are the common denominator |
| Concurrency | `asyncio`, `anyio`, `asyncer` | `Semaphore` for pool limits, `TaskGroup` for fan-out |
| TUI | `textual` | Agents view, streaming render, attach/detach |
| Schemas | `pydantic` v2 | Tool signature → JSON Schema for free |
| CLI | `typer` | |
| Config | `tomllib` (read) + `tomli-w` (write) | The UI writes config back; stdlib is read-only |
| Sandbox | `docker` SDK / devcontainer CLI | Phase 2+ |
| Search | `ripgrep` (pip wheel), `fd` (subprocess, optional) | Faster and more correct than anything hand-written. `ripgrep` is a declared dependency — the wheel drops an `rg` binary next to the interpreter, so `grep` works on a fresh checkout with no system packages. `fd` has no such wheel, so `glob` falls back to `rg --files` then `pathlib` |
| Tool schemas | `docstring-parser` | Signature + `Args:` block → JSON Schema, so a tool is described once |
| MCP | `mcp` (official SDK) | We adapt it; we do not reimplement the protocol |
| Web | `tavily` REST via `httpx` | No extra SDK — three endpoints behind one tool |
| Packaging | `uv` | |

**Explicitly not used: LangChain.** Also avoid LangGraph, CrewAI, and AutoGen. The whole
premise is programmatic control over context; a framework that owns the loop defeats it.

**Knowledge you need that isn't a library:**

1. **Jupyter messaging protocol** — `execute_request`, `iopub` streams, `execute_reply`.
   This is the most common place to get stuck. Read the spec before writing kernel code.
2. **SSE streaming + tool-call delta accumulation.** OpenAI streams
   `tool_calls[].function.arguments` in fragments; Anthropic uses `content_block_delta`.
   Normalize both in `core/providers/`, never above it.
3. **Async lifecycle** — cancellation, timeouts, graceful shutdown. Ctrl-C with five
   sub-agents in flight must leave no orphans and no half-written files.
4. **Token counting** — `tiktoken` for OpenAI-family; Anthropic exposes a count endpoint.
   Needed for compaction triggers and budget enforcement.
5. **Prompt caching** — keep system prompt and tool definitions byte-stable across turns.
   This is the single biggest cost lever.
6. **IPC** — Unix domain socket with JSONL framing for daemon ↔ CLI ↔ A2A.

---

## 9. Important Notices

**Arbitrary code execution.** The agent writes Python and runs it with your privileges.
Containerize before any autonomous run. Never enable `--autonomous` on the host.

**Total token cost goes up, not down.** Only the *main agent's* context shrinks. Sub-agent
tokens can be 3–5× the flat-loop total. Mitigate by routing sub-agents to cheaper tiers —
this is what `difficulty` exists for — while the main agent stays on a strong model.

**Latency increases in every measured environment.** The blog found this consistently, partly
because models do not reliably parallelize even when they can. For an interactive CLI this is
a real UX problem: stream progress and expose the agents view so the session never looks hung.

**No model has been trained on this scaffold.** Expect the agent to under-use `agent()` and
over-use plain REPL code. Compensate with explicit strategy prompts (the "env tips" pattern):
decompose → fan out → synthesize → iterate → commit `answer`.

**Kernel state bloats.** A three-hour session can accumulate gigabytes of variables. Enforce
a GC policy from day one: spill above a size threshold, LRU-evict, `del` unreferenced vars.
Run the collector asynchronously so it never blocks a turn.

**Reward hacking is real.** Prime Agent, given a refinement loop, discovered it could bypass
Factorio's rules via RCON and promoted *cheating* into a skill. The coding analogue is editing
the test to make it pass. Keep a verifier the agent cannot modify, and treat any test-file edit
during a "make tests pass" task as suspect.

**Debuggability must be designed in, not retrofitted.** Agent writes code → code spawns agent →
agent calls tool → tool returns a variable. When it goes wrong, there is no natural stack trace.
Structured logging and a trajectory viewer are Phase 1 requirements, not nice-to-haves.

---

## 10. Roadmap

| Phase | Scope | Folders | Done when |
| --- | --- | --- | --- |
| 0 | Shell: gateway + providers, config, CLI, chat/settings UI | `core/providers/`, `core/configs.py` | ✓ done — `stcode` runs, configures itself, and streams a flat reply |
| 1 | Single agent + IPython kernel + 6 core tools + 1 provider. `depth=0` | `core/kernel/` ✓, `core/harness/` ✓ | Can complete a two-file change end-to-end with a trajectory log — kernel and harness are in; the turn loop that drives them (`core/agent/`) is not |
| 2 | Async `agent()`, pool, gateway with fallback, `difficulty` routing | `core/agent/`, `core/providers/gateway.py` | Parallel fan-out works under a concurrency cap and a token budget |
| 3 | Daemon, socket IPC, A2A messaging, persistent sub-agents, agents view | `core/daemon/` | Detach/reattach mid-run; child survives parent compaction |
| 4 | Continual Harness — prompt/memory/skill/subagent CRUD, `/refine` | `core/harness/` | A repeated failure is promoted to a skill and reused next session |

Read the `prime-agent` source before starting Phase 2. It will save days.

---

## 11. Conventions

- Python 3.12+, `uv` for everything. `uv run pytest` before claiming anything works.
- Type hints mandatory in `core/` and `kernel/`. `mypy --strict` on those packages.
- Every tool is a pydantic model with a docstring — the docstring *is* the prompt the model sees.
- No blocking I/O inside the agent loop. If it can take 100 ms, it is `async`.
- Log every LLM call with model, difficulty, token counts, and cost. No exceptions.
- Commit messages: `area: what changed`. Keep them short.