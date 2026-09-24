# CLAUDE.md

The index. What this project is, what it bets on, the rules that are not negotiable, and
where the detail lives.

**Before you claim anything works:** `uv run pytest`.
**Before you write code:** §6, *How a change is made*.

---

## 1. What this is

`stcode` is a **general agent harness, specialised for coding**.

The unit of deployment is a **daemon**: a long-lived process holding one agent, speaking
JSONL over a socket. A TUI is just a client of it. That inversion — daemon first, UI
second — is what makes the container story real instead of aspirational.

One binary, three shapes, no implementation switches between them:

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
| Workspace | existing checkout at `cwd`, scope-enforced | agent runs `git clone` itself |
| Roles | none | BA / frontend-dev / backend-dev / devops … |
| Coordination | `task` tool (in-process sub-agent) | shared `/team` volume + `send_message` |
| Client | TUI over unix socket | TUI/web over TCP; attach and steer mid-run |
| Approval | human in the loop | `full-auto`, guarded by container detection |

**Non-goals.** Not a training harness. Not a reimplementation of MCP. Not feature parity
with Claude Code. No self-modifying prompts (rule 6).

---

## 2. The documentation

`docs/` is a mkdocs site. `uv run --group docs mkdocs serve` to read it; `mkdocs build`
is `strict`, so a broken internal link or a page missing from `nav` fails the build.

**Usage** — what a user reads.

| Section | What it covers |
| --- | --- |
| [docs/index.md](docs/index.md) | The front door |
| [getting-started/](docs/getting-started/) | Installation, quickstart, the config keys that matter on day one |
| [guide/](docs/guide/) | The three shapes, the TUI, approval modes, sessions, tools, skills, MCP, the REPL, tracing |
| [teams/](docs/teams/) | Setting a team up, roles, how one actually works |
| [sdk/](docs/sdk/) | Embedding an agent, writing a tool, the daemon protocol |
| [decisions/](docs/decisions/) | One page per question that could have gone another way |
| [contributing/](docs/contributing/) | **How a change is made** (§6), testing, releasing |

**Architecture** — what a contributor reads before changing something.

| Page | What it covers |
| --- | --- |
| [docs/architecture/index.md](docs/architecture/index.md) | The map: components, the diagram, dependency direction, repository layout |
| [agent-loop.md](docs/architecture/agent-loop.md) | `Agent` — the turn loop, events, interrupts, steering mid-turn, sub-agents |
| [harness.md](docs/architecture/harness.md) | Tools, `@tool`, permissions and approval modes, skills, roles, output elision |
| [providers.md](docs/architecture/providers.md) | `LLMGateway` — difficulty routing, retry, the unified wire format, prompt caching |
| [session.md](docs/architecture/session.md) | The append-only JSONL transcript, its three readers, and ids |
| [daemon.md](docs/architecture/daemon.md) | The socket, the protocol, `full-auto`'s container rule |
| [mcp.md](docs/architecture/mcp.md) | MCP servers as *code* rather than as tool definitions |
| [supervisor.md](docs/architecture/supervisor.md) | Stagnation detection at (almost) zero token cost |
| [team.md](docs/architecture/team.md) | Containers, roles, the shared volume, git integration — and the evaluation set |
| [tracing.md](docs/architecture/tracing.md) | Exporting the trajectory as OpenTelemetry spans |
| [configuration.md](docs/architecture/configuration.md) | Every section of `config.toml`, and what reads it |
| [README.md](README.md) | The repository's front door: install, run, the commands |

---

## 3. The four bets

Each is traceable to a measurement. Read the number before removing one.

**3.1 MCP tools are code, not tool definitions.** Other agents ship MCP schemas in the
prompt prefix every turn — 10–30k tokens for three servers, forever. We write them to
`.stcode/mcp_servers/<server>/<tool>.py` and let the agent `grep` and `import` what it
needs from the REPL. Anthropic measured the pattern at 150k → 2k tokens; measured here on
three servers and twelve tools, **9,869 → 0 chars** per turn (`smoke_mcp.py`). →
[mcp.md](docs/architecture/mcp.md)

**3.2 A supervisor watches the trajectory.** NVIDIA AVO hit 100% on ARC-AGI-3 and
credited system design, naming a supervisor that watches for stagnation. The session
JSONL is already the trajectory, so ours is just a reader: four heuristics at zero token
cost, and only a hit spends a `difficulty="low"` call that may answer NONE. The nudge is
appended as a `user` message, never a system-prompt edit, so caching survives. →
[supervisor.md](docs/architecture/supervisor.md)

**3.3 Agent-to-agent messaging has no protocol.** Containers share a volume, so a message
is a **file**: `send_message` writes JSON into `/team/inbox/<role>/`, and the receiver
drains its own directory. No registry, no routing table, no service discovery, no N²
socket mesh. Messages carry `refs` not content — enforced, not requested. →
[team.md](docs/architecture/team.md)

**3.4 Daemon-first, so containerization is not a rewrite.** Detach does not kill the
agent. Approval and questions are request–response correlated by `execution_id` over the
same JSONL framing. Transport (`unix` / `tcp`) is configuration, not architecture. →
[daemon.md](docs/architecture/daemon.md)

### Honest comparison

| | Claude Code | Cline / Kilo | **stcode** |
| --- | --- | --- | --- |
| Tool quality (read/edit/grep/bash) | excellent | good | comparable already |
| MCP | tool defs per turn | tool defs per turn | as code, 9.9k → 0 chars/turn |
| Runs headless in a container | partial | no | core design |
| Multi-agent team across containers | no | no | yes — but see §7 |
| Stagnation detection | no | no | yes |
| Observability export (OTel) | no | no | yes, off by default |
| Maturity, polish, ecosystem | **far ahead** | ahead | behind, and will stay behind |

We are not competing on polish. We are betting on the rows in the middle.

### Kept from other agents, and left behind

| Kept | Why |
| --- | --- |
| Exact-string `edit`, not line ranges or diffs | Line numbers go stale; a unique literal matches once or fails loudly |
| Read-before-write | A refused edit costs one turn; an overwritten unread file costs work that no longer exists |
| `todo_write` as a context device | Not a real tool. It keeps the plan in the window, and that is enough to earn its place |
| Elision with head and tail kept | The beginning says what ran, the end says how it went |
| A fresh process per `bash` call | Stateful shells make every failure irreproducible |
| Steering a running turn | Delivered at the next tool boundary — the only place a `user` message is legal |
| Four options on a question | Fits a card above the input, and forces the agent to prune its own list |

| Left behind | Why |
| --- | --- |
| A framework owning the loop (LangChain, LangGraph, CrewAI, AutoGen) | The premise is programmatic control over context and the loop |
| LLM-summarised tool output | Summarising loses information; eliding does not (rule 1) |
| MCP definitions in the prompt | The 9,869 → 0 measurement |
| A lead/orchestrator agent | You dispatch, per role. A `lead` would be one more agent profile |
| Self-refining prompts | Rule 6, and Prime Agent's promoted cheating skill (§7) |
| Notebook editing, multi-edit, image input | Deferred, not rejected |

---

## 4. Hard rules

Seven. Each exists because violating it produced a specific, known failure.

1. **Tool output is elided at its cap, never LLM-summarised.** Summarising loses
   information; eliding does not — *provided the full value is somewhere the model can
   reach*. `Runtime.outputs_reachable` is the single place that decides whether the hint
   may name `tool_out["…"]`, so the promise cannot drift from the fact.
2. **One agent = one role = one checkout = one merge boundary.** If two agents need to
   write the same file, the roles are split wrong. That is a design error, not a signal
   to add locking.
3. **`max_depth = 1`.** Sub-agents get neither `task` nor `repl`. Unbounded recursion plus
   no budget is a fork bomb.
4. **Sessions are append-only JSONL.** Never rewrite history. No branch, no fork, no leaf
   pointer — `cp session.jsonl` is the branching feature.
5. **`full-auto` without an approver runs only inside a container.** The daemon refuses to
   start otherwise. There is no override flag. This is code (`guard_autonomy`), not a
   warning in a document.
6. **The agent never edits its own harness.** No `/refine`, no prompt CRUD. Prime Agent
   shipped this and the agent promoted *cheating* into a skill (§7).
7. **Layers do not reach upward.** Concretely: the gateway emits `MessageStop(usage=…)`
   and the *agent* writes it to the session. A gateway that imports `Session` has taken a
   dependency on its own caller.

---

## 5. Tech stack

| Concern | Choice | Why |
| --- | --- | --- |
| REPL | plain `python -u` subprocess + JSONL | Persistent namespace, crash isolation, SIGINT, zero dependencies |
| LLM clients | `anthropic`, `openai`, `google-genai` (async) | One adapter each; normalise the wire format there and nowhere else |
| Concurrency | `asyncio`, `anyio`, `asyncer` | `gather` for fan-out, `Semaphore` for caps |
| TUI | `textual` | Streaming render, attach/detach |
| Schemas | `pydantic` v2 | Signature → JSON Schema for free |
| CLI | `typer` | |
| Config | `tomllib` (read) + `tomli-w` (write) | Stdlib is read-only; the UI writes config back |
| Search | `ripgrep` (pip wheel), `fd` (optional) | The wheel drops `rg` next to the interpreter, so `grep` works on a fresh checkout |
| Tool schemas | `docstring-parser` | Signature + `Args:` → JSON Schema, described once |
| MCP | `mcp` (official SDK) | We adapt it; we never reimplement the protocol |
| Tracing | `opentelemetry-sdk` + OTLP-HTTP, **optional extra** | Vendor-neutral; Langfuse/LangSmith/Phoenix all ingest it |
| Sandbox | Docker | Team mode; also what makes `full-auto` legal (rule 5) |
| Packaging | `uv` | |

**Explicitly not used: LangChain.** Also LangGraph, CrewAI, AutoGen. The premise is
programmatic control over context and the loop; a framework that owns the loop defeats it.

**Knowledge you need that isn't a library:** SSE streaming and tool-call delta
accumulation; prompt caching (keep the prefix byte-stable); async cancellation and
graceful shutdown; JSONL framing over sockets and pipes; token counting, when compaction
lands.

---

## 6. Conventions

### How a change is made

Six steps, in order. Full version, with the reasoning for each, in
[docs/contributing/workflow.md](docs/contributing/workflow.md).

1. **Analyse the spec, and ask.** Where two readings lead to materially different work,
   ask before building. Where a routine judgement call would do, make it and say so.
2. **Write the document.** The page a user would read — `docs/guide/` or `docs/sdk/`,
   or the matching `docs/architecture/` page — *before* the test and the code. A
   feature whose page is hard to write is a feature whose shape is wrong, and that
   costs a paragraph here instead of a rewrite three steps later. A question that could
   reasonably have gone another way gets a `docs/decisions/` record instead.
3. **Write the test.** From the document, not from the implementation. Name it after
   the failure it prevents.
4. **Implement.** The smallest change that makes the test pass and the document true.
5. **Run the tests.** `uv run pytest`, before claiming anything works. Still red? Say
   so, with the output.
6. **Commit.** `area: what changed`, short, with the docs in the same commit.

### The rest

- Python 3.10+ (CI runs 3.10–3.13; `core/common/compat.py` holds the only two backports), `uv` for everything. `uv run pytest` before claiming anything works.
- Type hints mandatory in `core/`. `mypy --strict` on it — installed, and **not yet
  clean**: ~35 errors, mostly provider SDK stubs. Documented in
  [docs/contributing/testing.md](docs/contributing/testing.md) rather than claimed.
- Tests live in `tests/`: `tests/core/<module>_test.py` for a module's public surface,
  `tests/integration/` for the flows the documentation describes, `tests/fakes.py` for
  the three test doubles, `tests/fixtures/workspace/` for the project they run against.
  Nothing calls a real provider except the two tests marked `live`, deselected by
  default.
- Every tool has a docstring, because **the docstring is the prompt the model sees**.
  Write it for the model: imperative, concrete, no rationale. Rationale goes in comments.
- No blocking I/O in the agent loop. If it can take 100 ms, it is `async`. The exception
  is `Session.append` — one line to an open file, deliberately sync.
- Commit messages: `area: what changed`. Keep them short.
- **When you change the architecture, update the page in `docs/architecture/` in the same
  commit.** A doc that has drifted from the code is worse than no doc: every agent that
  reads it builds in the wrong direction, and none of them will tell you.
- A **lazy import that exists only to hide a cycle** is a signal, not a solution: the
  thing being imported is in the wrong place. Move it down to `core/common/` if more
  than one package needs it, or into the one package that does.

---

## 7. Important notices

**Arbitrary code execution.** The agent runs commands and Python with your privileges.
Rule 5 is the mitigation and it is enforced in `guard_autonomy`, not by discipline.

**Multi-agent costs more, not less.** Anthropic measured multi-agent at **~15× the
tokens** of chat and single-agent at ~4×, with token volume explaining **80% of
performance variance**. They also list **"most coding tasks"** as a *poor* fit. Team mode
pays off only when roles work on genuinely separable things, which is why rule 2 draws the
boundary at the service, not the file.

**Latency increases.** Models do not reliably parallelise even when they can. Stream
progress, or a working session looks hung.

**No model is trained on this scaffold.** Expect under-use of `task`. Compensate in its
docstring: it must force an output format and a scope, because a vaguely-described
sub-agent is the number one cause of duplicated, off-target work.

**Reward hacking is real and documented.** Prime Agent, given a refinement loop, bypassed
Factorio's rules via RCON and **promoted cheating into a skill** — even when told not to.
The coding analogue is editing the test to make it pass; hence rule 6. If you revisit it,
the verifier must be outside the agent's write scope, and any test-file edit during a
"make the tests pass" task must be flagged.

**Debuggability is designed in, not retrofitted.** Agent → tool → sub-agent leaves no
natural stack trace. The session JSONL *is* the trace — keep every event in it, including
the ones the model never sees.

**Cost visibility.** Every LLM call lands in the session as a `usage` record. One place.
Do not add a second logger (rule 7); [tracing.md](docs/architecture/tracing.md) is an
export of that record, not a competitor to it.

---

## 8. Status

All ten build steps are done. Each smoke script drives real processes against a real
model.

| Phase | Gate | Evidence |
| --- | --- | --- |
| 0 — Shell | `stcode` runs, configures itself, streams a reply | — |
| 1 — The loop | a two-file change completes end to end with a readable session log | every `smoke_*.py` |
| 2 — Daemon + client | detach/re-attach mid-run works; `full-auto` refuses on the host | `smoke_daemon.py`, `tests/core/daemon_test.py` |
| 3 — Token economics | three MCP servers connected, prompt prefix does not grow | `smoke_mcp.py` — 9,869 → 0 chars |
| 4 — Autonomy | a deliberately looping task is caught and redirected | `smoke_supervisor.py` |
| 5 — Team | two containers, two roles, one feature shipped through `/team` | `smoke_team.py` — **partly** |

**The one open question.** The ~20 team-mode evaluation tasks in
[team.md](docs/architecture/team.md#the-evaluation-set) are written and **have not been
run**. Until they are, "does team mode help or merely spend 15× the tokens" is
unanswered — and §7 says that is a live question, not a rhetorical one. Running them is
the highest-value thing left.

**Later:** compaction with a visible threshold, sessions in a live database, provider
failover, `written_files` tracking, agent profiles that carry their own tools and MCP
servers alongside the prompt.
