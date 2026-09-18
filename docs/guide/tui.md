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

Five shapes, each one because it has to be told apart from the others at a glance.

| | |
| --- | --- |
| `───── attached to ~/.stcode/daemon.sock ─────` | **the platform acted** — connected, mode changed, session cleared. Full width, dim italic, centred. Not the model talking |
| `│ the middleware is probably in src/api` | **thinking.** Quoted and dim, so reasoning never reads as an answer. Capped at six lines while it streams — the last six — and scrollable afterwards |
| plain text | **the model's answer** |
| a box | **a tool call and its result** |
| `▌ no API key for anthropic` | **a warning or an error.** Full-width box, yellow or red |

Nothing else is announced. There is no `Config: /home/you/...` line and no
`Connecting…` — a config path you did not ask for is noise on every start, and a
connection that worked does not need a sentence. One rule when the daemon answers is
enough, and `?` has the paths when you want them.

### Tool calls

```
╭──────────────────────────────────────────────────────────────────╮
│ read │ path=src/api/mw.py                                        │
│      │ 1  from fastapi import Request                            │
│      │ 2  from .limits import Bucket                             │
╰──────────────────────────────────────────────────────────────────╯
```

The name, then a gutter, then the arguments on the first line and the result under them.
One box per call, so a turn with four parallel calls is four boxes and not a paragraph of
interleaved status lines.

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
| ++shift+enter++ | newline (++alt+enter++ also, for terminals that do not report shift+enter) |
| ++shift+tab++ | cycle the approval mode — takes effect on the **live** session |
| ++escape++ | interrupt the turn in flight, or close the open card |
| ++up++ / ++down++ | move through a card's options when one is open |

### Symbol aliases

Three characters do something when they open a card instead of being typed:

| | |
| --- | --- |
| `?` | on an empty input: the help card |
| `/` | at the start, or after a space: the command list |
| `@` | at the start, or after a space: files in the workspace, to mention |

All three **filter as you keep typing** — `/mo` narrows to `/model` and `/mode`, `@mw`
to `src/api/mw.py`. Six rows are visible and the list scrolls.

`/` and `@` are inserted as you type them — they are the start of a token, and `/mo` is
what is in the input. `?` is not: it is a toggle on an empty input, so it opens the card
and inserts nothing. **Typing it again inserts it** — `?` then `?` gives you a literal
question mark, which is what you wanted the second time, and the card gets out of the way
as soon as there is text.

++backspace++ closes any of the three. For `/` and `@` it also does the obvious thing:
delete back past the symbol and the token is no longer a token, so the card goes.

Filtering matches the **name**, not the description. Typing `/mod` offers `/model` and
`/mode` and not `/effort`, whose description happens to say "how hard the model should
think" — what you are typing is a name. `?` is where you look when you do not know the
name.

## Commands

| | |
| --- | --- |
| `/model` | provider, key and model. Applies to the **live session** as well as the next one |
| `/effort` | reasoning effort: `none` … `max`. Live session |
| `/mode` | approval mode, as a card |
| `/theme` | `auto`, `dark`, `light`, as a card |
| `/sessions` | earlier sessions, as a tree |
| `/connect` | point this client at a different daemon (`--daemonless` only) |
| `/mcp` | the MCP servers this session connected to, and their tools |
| `/skills` | the skills this session found |
| `/clear` | end this session and start a fresh one |
| `/help` | the help card — same as `?` |
| `/quit` | leave. The agent keeps working |

There is no command palette. ++ctrl+p++ does nothing, and the commands above are the
whole surface — a second, fuzzy way to reach the same eleven things is a second place for
them to drift.

### `/model` and `/effort` reach the running agent

Both write a `meta` record into the session file, and the agent reads its own meta before
every model call. So a model or an effort chosen mid-conversation applies to the **next
model call**, not the next session — and the transcript says when it changed, which is
the only way a session that used two models is readable afterwards.

Sessions are append-only, so nothing is rewritten: a later `meta` record overrides an
earlier one. → [Sessions](../architecture/session.md#meta-is-a-merged-view)

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
│   └ api-scout                                                    │
│   └ test-writer                                                  │
│ 01M2Q8…  1h ago   ~/w/stcode      fix the flaky daemon test      │
│ 01M2NN…  yesterday  ~/w/other     (no messages)                  │
╰──────────────────────────────────────────────────────────────────╯
```

Built from each file's `meta` line — `parent` and `agent_name` are already in there, so
the tree is a group-by and not an index. **Only a parent is selectable.** A sub-agent's
session is a transcript to read, not a conversation to continue: it has no user on the
other end of it, and pushing a message into one would be talking to something that was
built to answer exactly one question.

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

Parallel tool calls can raise two requests at once. They **queue**, one card at a time,
answered oldest first; the rest of the turn keeps streaming behind them.

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
| `~/.stcode/ui.toml` | the **terminal's** preferences: the theme, and the folders you have trusted |

```toml
# ~/.stcode/ui.toml
theme = "auto"                       # auto | dark | light
trusted = ["/home/you/work/stcode"]
```

A headless daemon has no theme and trusts nothing; putting either in `config.toml` would
put a fact about your terminal into the file a container reads.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

Neither path is something to memorise — `?` shows both, along with the session file the
current conversation is writing to.

## Streaming and steering

Text streams as the model produces it. You can type while it works — a message sent
mid-turn is delivered at the next tool-call boundary, never spliced into the model call
in flight. → [The agent loop](../architecture/agent-loop.md)
