# The TUI

A [textual](https://textual.textualize.io/) app that is, architecturally, just a client
of the daemon. Everything it can do is a protocol message.

## The screen

```
                ███████╗████████╗ ██████╗ ██████╗ ██████╗ ███████╗
                ██╔════╝╚══██╔══╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
                ███████╗   ██║   ██║     ██║   ██║██║  ██║█████╗
                ╚════██║   ██║   ██║     ██║   ██║██║  ██║██╔══╝
                ███████║   ██║   ╚██████╗╚██████╔╝██████╔╝███████╗
                ╚══════╝   ╚═╝    ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝

────────────────────── session in ~/w/stcode ───────────────────────

 you  Add rate limiting to /v1/search

 │ The middleware is probably under src/api. Grep before reading.

 I'll start with the middleware.

╭──────────────────────────────────────────────────────────────────╮
│ grep │ pattern=middleware path=src                               │
│      │ src/api/mw.py:12:def middleware(request):                 │
╰──────────────────────────────────────────────────────────────────╯

╭─ commands ───────────────────────────────────────────────────────╮
│ /model      provider, key and model                              │
│ /effort     reasoning effort                                     │
╰──────────────────────────────────────────────────────────────────╯
> Ask stcode…
  model claude-opus-5   provider anthropic   mode suggest
```

Four rows, top to bottom: **banner**, **transcript**, a **card** when one is open, the
**input**, and the **status line** under it.

The status line is under the input, not above it, because that is the order you read in:
you look at what you are typing, and the model and mode are the footnote to it. In
`--daemonless` it also carries the daemon's address — the one shape where *which agent
am I talking to* is a live question.

### The banner

Shown centred on an empty transcript and **hidden the moment the transcript is long
enough to scroll**. It is a greeting, not furniture: once there is a conversation, the
conversation is what the screen is for. No tagline under it.

## Themes

Two themes, both with `#78b032` as the primary colour: `stcode-dark` and `stcode-light`.

The default is **auto**: stcode asks the terminal for its background colour (an `OSC 11`
query) before the app starts, and picks the theme that matches. A terminal that does not
answer within 200 ms, or a redirected stdout, falls back to dark. `COLORFGBG` is read
first when it is set, since terminals that export it answer faster than they answer a
query.

`/theme` overrides it with `auto`, `dark` or `light`, and the choice is remembered in
`~/.stcode/ui.toml` — see [what the TUI owns](#what-the-tui-owns).

## Trusting a folder

The first time stcode runs in a directory it asks:

```
╭─ trust this folder? ─────────────────────────────────────────────╮
│                                                                  │
│  /home/you/work/some-repo                                        │
│                                                                  │
│  The agent will read, write and run commands here with your       │
│  privileges.                                                     │
│                                                                  │
│                                      [ Cancel ]   [ Trust ]      │
╰──────────────────────────────────────────────────────────────────╯
```

**Cancel exits.** There is no "continue untrusted" — the agent's whole job is running
things in this directory, so a session you have not trusted is a session with nothing to
do. Trusted paths are remembered in `~/.stcode/ui.toml`.

`--daemonless` skips it. The workspace belongs to the daemon's machine, not this one, so
there is nothing here to trust; you get the `/connect` modal first instead.

## Reading the transcript

Eight shapes, each one because it has to be told apart from the others at a glance.

| | |
| --- | --- |
| `───── attached to ~/.stcode/daemon.sock ─────` | **the platform acted** — connected, mode changed, session cleared. Full width, dim italic, centred. Not the model talking |
| a tinted block | **what you said.** Tinted so that scrolling back to find where you asked something is looking rather than reading |
| `│ the middleware is probably in src/api` | **thinking.** Quoted and dim, so reasoning never reads as an answer. Capped at six lines while it streams — the last six — and scrollable afterwards |
| dim, unquoted | **a running tool's output**, while it is still running: a REPL cell printing, a URL being fetched. Last six lines, advisory, never in the session |
| plain text | **the model's answer** |
| a green or red box | **a tool call and its result.** The colour is the outcome |
| a rule down the left | **a `!` command you ran.** Never in the session |
| `▌ no API key for anthropic` | **a warning or an error.** Full-width box, yellow or red |

Nothing else is announced. There is no `Config: /home/you/...` line and no
`Connecting…` — a config path you did not ask for is noise on every start, and a
connection that worked does not need a sentence. One rule when the daemon answers is
enough, and `?` has the paths when you want them.

### Tool calls

```
╭─ read ───────────────────────────────────────────────────────────╮   green
│ path=src/api/mw.py                                               │
│ 1  from fastapi import Request                                   │
│ 2  from .limits import Bucket                                    │
╰──────────────────────────────────────────────────────────────────╯

╭─ bash ───────────────────────────────────────────────────────────╮   red
│ command=pytest -q                                                │
│ 3 failed, 12 passed                                              │
│ …                                                                │
╰──────────────────────────────────────────────────────────────────╯
```

The name is the box's **heading**, the arguments and the result are grey under it, and
one box per call — so a turn with four parallel calls is four boxes and not a paragraph
of interleaved status lines.

**The colour carries the outcome, so nothing else has to.** Dim while the call is
running, green when it worked, red when it did not. There is no `✗` and no second word
for the same fact, which means a turn can be skimmed by colour alone.

The result is trimmed to **two lines**, with a `…` when there was more. The box is a
receipt, not a viewer: what you want from a finished call is that it ran and whether it
worked, and six calls each showing six lines is a screenful of output with the
conversation pushed off the top of it. The whole value is in the session — and the
`tool_finished` frame only carries 240 characters of it anyway.

### Sub-agents

A `task` sub-agent renders in exactly the same shapes, with its name in a left gutter and
its content indented:

```
api-scout │ Three handlers, all in src/api/mw.py.
api-scout ╭────────────────────────────────────────────────────────╮
          │ grep │ pattern=Bucket                                  │
          ╰────────────────────────────────────────────────────────╯
```

The colour is picked from the name, not at random: the same sub-agent is the same colour
in every session and after every restart, which is the only way the colour tells you
anything.

Its events are **observational**. A sub-agent's transcript is its own file
([sessions](sessions.md)), and what you are watching here is a live window onto it —
nothing in the parent's history changes because you watched.

## Typing

The input is a text area, not a single line.

| | |
| --- | --- |
| ++enter++ | send |
| ++ctrl+j++ | **newline** |
| ++shift+enter++ / ++alt+enter++ | newline, on terminals that report them — see below |
| ++shift+tab++ | cycle the approval mode — takes effect on the **live** session |
| ++escape++ | interrupt the turn in flight, or close the open card |
| ++up++ / ++down++ | move through a card's options when one is open |

!!! note "Why ++ctrl+j++ and not ++shift+enter++"

    ++shift+enter++ and ++alt+enter++ only exist on the wire when your terminal speaks
    the [kitty keyboard protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/).
    stcode asks for it on start-up; a terminal that does not answer sends plain `CR` for
    shift+enter and `ESC CR` for alt+enter, and **both of those are indistinguishable
    from a bare ++enter++** — so on those terminals the obvious newline keys send your
    half-written message instead.

    ++ctrl+j++ is `LF`. It is a different byte from `CR` on every terminal there is, so
    it always works. Both spellings are wired up; this is the one to reach for.

### Symbol aliases

Four characters do something instead of being an ordinary line of text:

| | |
| --- | --- |
| `?` | on an empty input: the help card |
| `/` | at the start, or after a space: the command list |
| `@` | at the start, or after a space: files in the workspace, to mention |
| `!` | at the start of the line: run the rest of it in your shell — see [`!`](#running-a-command-yourself) |

The first three **filter as you keep typing** — `/mo` narrows to `/model` and `/mode`, `@mw`
to `src/api/mw.py`. Six rows are visible and the list scrolls.

The first `@` of a session lists the workspace in a thread, so it can open a beat before
it has anything to show. It says `listing the workspace…` and fills itself in when the
listing arrives — there is nothing to retype.

`/` and `@` are inserted as you type them — they are the start of a token, and `/mo` is
what is in the input. `?` is not: it is a toggle on an empty input, so it opens the card
and inserts nothing. **Typing it again inserts it** — `?` then `?` gives you a literal
question mark, which is what you wanted the second time, and the card gets out of the way
as soon as there is text.

++backspace++ closes any of the three cards. For `/` and `@` it also does the obvious thing:
delete back past the symbol and the token is no longer a token, so the card goes.

Filtering matches the **name**, not the description. Typing `/mod` offers `/model` and
`/mode` and not `/effort`, whose description happens to say "how hard the model should
think" — what you are typing is a name. `?` is where you look when you do not know the
name.

## Commands

| | |
| --- | --- |
| `/model` | provider, key, model **and routing**. Applies to the live session as well as the next one |
| `/effort` | reasoning effort: `none` … `max`. Live session, and remembered |
| `/mode` | approval mode, as a card |
| `/theme` | `auto`, `dark`, `light`, as a card |
| `/token` | what this session has spent, as a card |
| `/sessions` | earlier sessions, as a tree |
| `/connect` | point this client at a different daemon (`--daemonless` only) |
| `/mcp` | the MCP servers this session connected to, and their tools |
| `/skills` | the skills this session found. Choosing one writes its name into the input |
| `/<skill>` | run a skill: `/pdf split page 3 out` |
| `/clear` | end this session and start a fresh one |
| `/help` | the help card — same as `?` |
| `/quit` | leave. The agent keeps working |

There is no command palette. ++ctrl+p++ does nothing, and the commands above are the
whole surface — a second, fuzzy way to reach the same twelve things is a second place for
them to drift.

### Skills are commands

Every skill this session found is on the `/` list under the built-in commands, so `/pdf`
is one keystroke and one enter rather than a sentence to compose. `/skills` is the same
set with the descriptions, and **choosing one writes `/name` into the input rather than
sending it** — a skill usually needs a sentence after it saying what to do, and a card
that fired on enter would leave nowhere to put it.

```
/agent-browser check that the login page still works on mobile
```

What reaches the agent is an ordinary message naming the skill, so everything after the
name is the task, passed through untouched. A skill cannot shadow a built-in command: if
you install one called `clear`, `/clear` still ends the session.

A name that is neither a command nor a skill is still an error — skills joining the list
does not turn a typo into a message to the model.

### `/model` sets the routing too

The setup screen is where `[routing]` lives now, under the credentials:

```
Model              claude-opus-5
Difficulty         high  —  the best model you configured
Routing
  low              claude-haiku-4-5
  medium           claude-sonnet-5
  high
Requests at once   0 — no cap. Set 1 for Ollama or another local endpoint.
```

A blank tier follows the Model field, which is what the screen did before any of these
fields existed — so you never have to fill them in to keep what you had. **Difficulty**
is which tier the main agent itself runs at; sub-agents and the supervisor choose their
own. → [Providers and the gateway](../architecture/providers.md)

**Requests at once** is the one to change if you run a local model. Five parallel
sub-agents are five simultaneous completions against the same server, and a machine
holding one 27b model on one GPU answers that with a 503 rather than five answers. `1`
makes them run one after another.

### `/token`

```
╭─ tokens ─────────────────────────────────────────────────────────╮
│ input        48,113                                              │
│ output       6,204                                               │
│ cache read   31,900 — charged at a fraction of input             │
│ total        54,317                                              │
│ model calls  19                                                  │
╰──────────────────────────────────────────────────────────────────╯
```

Summed from the session's own `usage` records — one per model call — and asked for
fresh each time, which is the only way it can be right. A turn with six tool calls made
seven requests, `turn_finished` carries the last of them, and a terminal that attached
halfway through watched half a conversation. The daemon has all of it; nothing else
does.

Cache reads are shown when there are any, because "turn two is cheaper" is otherwise a
claim you have to take on faith.

A sub-agent's spending is in **its** session, not here — the same boundary that keeps
its tool output out of the parent's context. → [Sessions](sessions.md)

### `/model` and `/effort` reach the running agent

Both write a `meta` record into the session file, and the agent reads its own meta before
every model call. So a model or an effort chosen mid-conversation applies to the **next
model call**, not the next session — and the transcript says when it changed, which is
the only way a session that used two models is readable afterwards.

Sessions are append-only, so nothing is rewritten: a later `meta` record overrides an
earlier one. → [Sessions](../architecture/session.md#meta-is-a-merged-view)

`/effort` is also written to `[defaults] reasoning_effort`, so the choice survives the
session. `/model` writes the whole provider block, and — when this terminal started the
daemon — hands the new credentials to the gateway that the running agent is already
holding, which is what makes a pasted key work without restarting.

In `--daemonless` it does not: the daemon reads its own config file, on its own machine,
and this terminal has no business replacing the credentials a container was started
with. The provider and model still reach the session, and the status line says as much.

Sub-agents do not inherit it. `task(difficulty="low")` is a routing decision the model
made about its own work, and a `/model` override that silently upgraded every scout to
Opus would make difficulty tiers meaningless.

### `/clear` starts a new session

It does not erase anything. The current session is left as it is on disk and a new one
begins — and because a session file is not created until its first message, `/clear`
twice in a row leaves no debris at all.

This is the one thing in the UI that people expect to be destructive and is not.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

### `/sessions` shows a tree

```
╭─ sessions ───────────────────────────────────────────────────────╮
│ 01M2QF…  2m ago   ~/w/stcode      rate limiting                  │
│   └ api-scout    01M2QG4KX8ZB1T…                                 │
│   └ test-writer  01M2QG4M02PN7R…                                 │
│ 01M2Q8…  1h ago   ~/w/stcode      fix the flaky daemon test      │
│ 01M2NN…  yesterday  ~/w/other     (no messages)                  │
╰──────────────────────────────────────────────────────────────────╯
```

Built from each file's `meta` line — `parent` and `agent_name` are already in there, so
the tree is a group-by and not an index. A sub-agent's row carries its **id** beside its
name, because the name is not unique — two turns can each spawn an `api-scout` — and the
id is what you need in order to open the transcript. **Only a parent is selectable.** A sub-agent's
session is a transcript to read, not a conversation to continue: it has no user on the
other end of it, and pushing a message into one would be talking to something that was
built to answer exactly one question.

## Running a command yourself

`!` at the start of the line runs the rest of it in your shell:

```
▌ ! git status
▌ On branch main
▌ nothing to commit, working tree clean
```

**None of it touches the agent.** It is not a tool call, it is not approved, and it is
not written to the session — so `!git diff` before describing a change costs no context
and leaves nothing the model will later read back as something it did. The rule down the
left is there for that reason: it must be impossible to mistake for the agent's work.

It runs in **this** terminal's workspace. In `--daemonless` the agent's workspace is a
different machine, and `!` is always the near one — which is the honest answer, because
this is your shell, not the agent's.

Ten seconds, then it is killed. Raise it in `~/.stcode/ui.toml`:

```toml
shell_timeout = 30    # seconds; clamped to 1–120
```

The ceiling is low on purpose. `!` runs on the UI's budget rather than the agent's, and
a command that needs two minutes belongs in a second terminal — or in a `bash` tool call
where the agent can watch it and act on the result.

## Approvals and questions

Both arrive as a **card above the input**, not a modal:

```
╭─ approve? ───────────────────────────────────────────────────────╮
│ bash (execute)                                                   │
│ rm -rf build/                                                    │
│                                         n deny      y approve    │
╰──────────────────────────────────────────────────────────────────╯
```

A modal covers the transcript, which is exactly the thing you need to read in order to
answer — *why* is it running this? A card leaves it on screen.

**What you are approving is bounded at ten lines**, head and tail, with a count of what
is between them:

```
╭─ approve? ───────────────────────────────────────────────────────╮
│ repl (execute)                                                   │
│ import asyncio                                                   │
│ from mcp_servers.context7 import resolve_library_id              │
│ … 31 more lines …                                                │
│ asyncio.run(main())                                              │
│                                         n deny      y approve    │
╰──────────────────────────────────────────────────────────────────╯
```

The card is laid out top to bottom, so a body that grows without limit pushes **deny**
and **approve** off the bottom of the screen — a card that asks a question and shows no
way to answer it. Elided rather than summarised, like any other output: the head says
what the cell is about to do, the tail says what it leaves behind, and the count says how
much you are not being shown.

Parallel tool calls can raise two requests at once. They **queue**, one card at a time,
answered oldest first; the rest of the turn keeps streaming behind them — and the next
one appears the moment you answer the one in front of it.

Everything else is unchanged and still true:

- **Whoever answers first decides.** If two terminals are watching, either can approve.
- **A denial is a denial, not a failure.** The tool is told the user declined, and the
  model is expected to adapt rather than retry.
- ++escape++ denies, and so does walking away — the default is deny, because ++enter++
  on a dialog you have not read should not be how an `rm -rf` gets run.

Questions (`ask_user_question`) come as the same card with up to four options and a free
text field that stays open, because the tool's own docstring promises the model that the
user can always answer something else.

## What the TUI owns

Two files, and the split matters:

| | |
| --- | --- |
| `~/.stcode/config.toml` | the **agent's** configuration: providers, keys, routing, limits. Read by the daemon, including one in a container |
| `~/.stcode/ui.toml` | the **terminal's** preferences: the theme, the folders you have trusted, and the `!` timeout |

```toml
# ~/.stcode/ui.toml
theme = "auto"                       # auto | dark | light
trusted = ["/home/you/work/stcode"]
shell_timeout = 10                   # seconds a `!` command may run; clamped to 1–120
```

A headless daemon has no theme, trusts nothing and never runs a `!`; putting any of the
three in `config.toml` would put a fact about your terminal into the file a container
reads.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

Neither path is something to memorise — `?` shows both, along with the session file the
current conversation is writing to.

## Streaming and steering

```
⠹ working   model claude-opus-5   provider anthropic   mode suggest
```

The status line spins from the moment you press ++enter++, not from the first token. The
gap between them can be seconds — a cold local model, a long prompt, a retry — and until
something moves, a screen where the message landed looks exactly like one where it did
not. The spinner answers the only question anybody has in that gap.

Text streams as the model produces it. You can type while it works — a message sent
mid-turn is delivered at the next tool-call boundary, never spliced into the model call
in flight. → [The agent loop](../architecture/agent-loop.md)
