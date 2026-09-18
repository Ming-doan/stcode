# The harness

`core/harness/`. One agent's capabilities: its tools, its skills, its prompt, its
permissions. The agent loop needs two verbs from here — *what do I advertise* and *run
this* — and nothing about how either is built.

```py
harness = await Harness.create(cwd=root, approval_mode="auto-edit", role="backend-dev")
system  = harness.system_prompt()
tools   = harness.tool_definitions()
result  = await harness.invoke("read", {"path": "src/main.py"}, tool_call_id=call.id)
```

A `Harness` is **per agent**, not per process: it holds a cwd, a write scope and an
approval mode, and two sub-agents must be able to have different scopes at once.
`for_subagent()` makes the narrowed copy — mutating one harness between spawns is the
bug that rule exists to prevent.

`invoke()` **never raises** for a tool-level failure. Bad arguments, a denial, a timeout,
a bug inside the tool — all come back as `ToolResult(is_error=True)`, because the loop's
only move after a tool call is to hand a `tool_result` back. A raise takes down the turn.

## Layout

```
core/harness/
  harness.py        the facade that composes everything below
  approvals.py      ApprovalMode, ToolPermission, and the mode policy
  errors.py         the ToolError family — a leaf, so nothing cycles
  context.py        HarnessContext — cwd, scope, reads, todos, git, repl, shells
  mcp.py            MCP servers → generated code, or advertised tools
  tools/
    base.py         @tool, Tool, Runtime[T] — the parameter the model cannot see
    schema.py       signature + docstring → JSON Schema
    registry.py     which tools exist, and which an agent may see
    files.py search.py shell.py repl.py web.py todo.py interact.py skill.py
  prompts/          the plan and execute prompts, and the sub-agent briefing
  skills/           SKILL.md discovery, loaded on demand
```

`registry.py` sits beside `base.py` and `schema.py` because it belongs to the tool
subsystem: it knows what a `Tool` is, and `Harness` knows it. `approvals.py` stays a
level up — `configs.py`, `cli/` and `core/daemon/` all import `ApprovalMode`, and it is
vocabulary, not tool internals.

## `@tool` — the signature is the schema

```py
@tool(permission=ToolPermission.WRITE)
async def write(path: str, content: str, runtime: Runtime[HarnessContext]) -> str:
    """Write `content` to `path`, creating parent directories as needed.

    Args:
        path: File to write. Absolute, or relative to the session's working directory.
    """
```

* **The signature is the schema, the docstring is the description.** Both are read off
  the function by `schema.py`, so a tool's advertised interface cannot drift from its
  code. `Annotated[str, Field(description=...)]` beats the `Args:` block when both exist.
* **`Runtime` is the parameter the model cannot see.** Matched on the annotation, not the
  name; filtered out of the schema before validation, so a model that tries to pass one
  is rejected rather than quietly obeyed. A model that can see `approval_mode` is a model
  that will try to set it.
* **Pydantic `title` keys are stripped** everywhere. They restate the key they are filed
  under, and those tokens sit in every turn's cached prefix.

Three ways to call one:

```py
write.to_tool_definition()                       # the schema a provider advertises
await write.invoke({"path": ...}, runtime=rt)    # gated, validated, never raises
await write(path, content, runtime=rt)           # a plain Python call, raises normally
```

## Permissions: declared once, enforced elsewhere

`@tool(permission=...)` states the class of side effect. `requires_approval(mode, perm)`
and `is_forbidden(mode, perm)` in `approvals.py` decide what that means under the current
mode. **A tool never tests the mode itself** — one that knows about `full-auto` will
eventually disagree with the next one about what it means.

| Permission | Means |
| --- | --- |
| `READ` | observes the workspace. Safe in every mode, `plan` included |
| `WRITE` | mutates the filesystem. The thing `plan` exists to forbid |
| `EXECUTE` | an arbitrary command or arbitrary Python. Strictly wider than `WRITE` |
| `NETWORK` | egress. Gated *separately* — the risk is disclosure, not corruption |
| `INTERACTIVE` | asks the human something. Always allowed: a mode that stops the agent asking has the gate backwards |

| Mode | Read | Write | Execute | Network |
| --- | --- | --- | --- | --- |
| `plan` | yes | **forbidden** | **forbidden** | ask |
| `suggest` | yes | ask | ask | ask |
| `auto-edit` | yes | yes | ask | yes |
| `full-auto` | yes | yes | yes — **container only**, enforced in `guard_autonomy` | yes |

Each mode adds one class of *workspace mutation* to the previous: nothing, then writes,
then arbitrary execution. `NETWORK` sits outside that ladder, which is why `plan` asks
for it rather than refusing: reading a web page only changes what the agent knows.
"Forbidden" and "ask" are different answers, and the agent is told which it hit —
retrying a forbidden tool is never going to work.

**Tools a mode forbids are never advertised.** Being offered a capability and then
refused it wastes a turn and reads, to the model, like a bug to work around.

`permission_for` narrows the class *per call*, from the validated arguments — `bash` is
the one that needs it, because `git status` and `rm -rf build/` arrive through the same
tool. A call may narrow what it claims, never widen it.

## The tools

| Tool | Signature | Notes |
| --- | --- | --- |
| `read` | `read(path, offset=0, limit=None)` | line-numbered; 2000-line default, 32 KB before eliding |
| `write` | `write(path, content)` | scope-gated; refuses a file this session has not read |
| `edit` | `edit(path, old, new)` | exact unique match; fails loudly on 0 or >1 |
| `bash` | `bash(command, timeout=120, background=False, cwd="", description="")` | **each call is a fresh process**, and the docstring says so |
| `bash_output` | `bash_output(shell_id, kill=False)` | drains a background shell, new output only |
| `glob` | `glob(pattern, path=".")` | `fd`, falling back to `rg --files`, then `pathlib` |
| `grep` | `grep(pattern, path=".", ...)` | ripgrep. Do not hand-roll |
| `ls` | `ls(path)` | `.gitignore`-aware |
| `todo_write` | `todo_write(items)` | not a real tool — a device to keep the plan in context. Keep it |
| `ask_user_question` | `ask_user_question(question, options=None, header="", multi_select=False)` | fails clearly with no user attached, rather than hanging |
| `skill` | `skill(name)` | loads a `SKILL.md` body on demand |
| `repl` | `repl(code, timeout=120)` | persistent namespace; top-level agents only |
| `web_search` | `web_search(query=None, url=None)` | needs `TAVILY_API_KEY`; says so on the first call if missing |
| `task` | `task(prompt, name, tools=None, scope=None, difficulty=...)` | sub-agent — lives in `core/agent/` |
| `send_message` | `send_message(to, subject, body, refs=None)` | team mode — lives in `core/team/` |

**Read before write** is one design, not three: `read` records what it saw, `write` and
`edit` refuse a file this session has not read and warn when it changed underneath them.
The cost is asymmetric — a refused edit costs one turn, an overwritten file the agent
never read costs work that no longer exists.

**`options` is capped at four** on `ask_user_question`. Four fits a terminal modal,
stays scannable, and maps to number keys; "something else" is always available, so
nothing is lost. Above four the agent is handing over its search space instead of doing
the pruning it is there to do — a question with six real answers is usually two
questions.

### Named sets

`MAIN_TOOLS` is a top-level agent's allowance; `WORKER_TOOLS` is a sub-agent's — no
`repl`, no `task`, no `send_message`. `READ_ONLY_TOOLS` is derived from the declared
permissions, so it cannot drift. Registered is not advertised: a tool with no working
backend stays out of every set.

Two tools are added from *outside* the harness, both as factories closing over something
`core/harness` must not import: `task` over an `Agent`, `send_message` over a `Mailbox`.

### Choosing the set from config

```toml
[agent]
tools = ["read", "grep", "glob", "ls"]   # an allow-list replacing the default set
exclude_tools = ["web_search"]           # subtracted from whatever is left
```

Applied by `apply_tool_policy` **after** the agent is fully assembled, so `task`,
`send_message` and MCP tools can be named too — and so the operator's word outranks a
feature switch that added one. A name matching no tool raises at startup with the list of
real ones: a config that quietly produced a smaller tool set would be diagnosed as "the
model is ignoring its tools", weeks later.

## Output: elided, never summarised

Tool output is capped per tool and elided in the middle — head and tail kept, because the
beginning says what ran and the end says how it went.

| Tool | Cap |
| --- | --- |
| `read` | 32768 — file contents are the ground truth, and a half-seen function is worse than a slow turn |
| `web_search` | 24576 |
| `bash`, `bash_output`, `grep`, MCP tools | 16384 |
| `skill` | 65536 — authored content, loaded deliberately |
| everything else, including `repl` | 8192 (`DEFAULT_VIEW_LIMIT`) |

**Eliding loses nothing only if the full value is somewhere the model can reach.** So the
cut half is parked in `harness.outputs` under an `output_id`, `Harness.invoke` injects it
into the REPL's `tool_out`, and the hint in the elision marker names
`tool_out["read_ab12"]` — *but only when a REPL is actually attached*. That decision
lives in exactly one place, `Runtime.outputs_reachable`, so the promise cannot drift from
the fact. Without a REPL the hint says to re-call with a narrower range instead.

`outputs` is an `OutputStore` (`core/harness/outputs.py`): newest wins, oldest evicted, 32
entries or 8 MB of text. Nothing else ever removed an entry, so a long session used to
hold every large result it had ever produced.

## Prompts

`core/harness/prompts/`. One axis — **mode**: `plan` researches and cannot write,
`execute` makes changes. It is derived from the approval mode by `mode_for()`, in one
function, so the prompt and the permission gate cannot disagree. A prompt saying "make
the change" while `approvals.py` forbids writes produces an agent that spends its turn
discovering it may not work.

Sections are Python constants, not package data: a prompt that goes missing at runtime is
a session that behaves subtly differently with no error.

**Order is load-bearing.** Caching works on a byte-stable prefix, so static sections come
first and never vary within a session, and everything turn-varying (tool list, todos, git
state, cwd) goes last. Adding a dynamic value to an early section silently doubles the
cost of every turn.

`subagent=True` is derived from `depth`, never passed in: a harness from `for_subagent()`
is a sub-agent by construction, and a caller allowed to say otherwise will get it wrong.

## Roles are files on disk, not package data

```
.stcode/agents/<role>.md          # this project's
~/.stcode/agents/<role>.md        # this machine's
$STCODE_AGENTS_DIR/<role>.md      # one directory and nothing else — what a container mounts
```

Searched in that order by `load_role()`. An unknown name **raises**, with the list of
what is installed: a container started with a typo'd role, or with its agents volume
unmounted, must refuse rather than run an agent that owns nothing. There is deliberately
no bundled fallback — a silent one would mean that container starts anyway, as somebody
else's backend dev.

The repository ships four to copy in `examples/agents/`. A role states what it owns,
whose output it reads, and who it reports to; the body is passed into the prompt
unchanged, because a role is data and rewriting it here would make it code again.

## Skills

`SKILL.md` files discovered under the workspace. Only the catalogue — name and one line
each — goes in the prompt; `skill(name)` loads a body on demand. The same economics as
MCP-as-code: a capability you might use should not cost tokens every turn.
