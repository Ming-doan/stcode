# stcode

A coding agent for the terminal, built the other way round: **the agent lives in a
daemon, and the UI is a client of it.**

That one inversion is the whole design. Closing the window does not stop the work.
Several terminals can watch the same session. Moving the agent into a container is a
config key — `transport = "tcp"` — rather than a rewrite. And because a container is a
real boundary, `full-auto` is allowed to exist without being a foot-gun: the daemon
refuses to start in that mode anywhere its blast radius is not contained.

> **Status: phase 2 of 5.** The turn loop, the tools, the session transcript, the
> daemon and the TUI client all work. MCP-as-code, the stagnation supervisor, and
> multi-container team mode are designed and not yet built — see
> [`CLAUDE.md`](CLAUDE.md) §12 for exactly what exists and what does not. That file is
> deliberately honest about the difference; so is this one.

---

## Quickstart

```bash
uv sync                      # Python 3.12+
uv run stcode                # first run opens a one-time setup screen
```

Pick a provider, paste an API key (or leave it blank and export `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `GEMINI_API_KEY`), choose a model, and type.

```bash
uv run stcode                        # UI + a daemon, started here if none is listening
uv run stcode --headless             # the daemon alone — what a container runs
uv run stcode --daemonless           # the UI alone, attached to a daemon elsewhere

uv run stcode sessions               # recent sessions, newest first
uv run stcode --resume 01HX…         # attach to one, live or from disk
uv run stcode config                 # where config lives and what it selects
```

Useful flags: `--cwd` (workspace for the session), `--mode` (`plan` | `suggest` |
`auto-edit` | `full-auto`), `--model`, `--config`, and `--transport` / `--socket` /
`--host` / `--port` to point at a daemon for this run without editing the config file.

### Attaching to an agent in a container

```bash
# in the container
docker run -e STCODE_SANDBOX=1 -e ANTHROPIC_API_KEY -p 7717:7717 \
    -v "$PWD:/workspace" stcode --headless --transport tcp --host 0.0.0.0

# on your machine
uv run stcode --daemonless --transport tcp --host <container-ip> --port 7717
```

`--daemonless` never starts an agent locally: if nothing answers, it asks *which
daemon?* rather than quietly running the work on your laptop instead. `/connect` moves
an already-running UI to a different one.

### In the UI

| | |
| --- | --- |
| `shift+tab` | cycle approval mode — takes effect on the live session |
| `esc` | interrupt the turn in flight |
| `f2` / `/model` | provider, key, model |
| `/connect` | point this client at a different daemon |
| `/mode`, `/clear`, `/help`, `/quit` | |

---

## What it does differently

Four things, each traceable to a measurement rather than a preference. The full argument
— with the numbers — is in [`CLAUDE.md`](CLAUDE.md) §2.

**MCP tools as code, not tool definitions** *(designed, step 8)*. Most agents ship MCP
schemas in the prompt prefix on every turn: 10–30k tokens, forever. stcode writes them
to `.stcode/mcp_servers/<server>/<tool>.py` and lets the agent `grep` and import what it
needs. Anthropic measured this pattern at 150k → 2k tokens.

**A supervisor that watches the trajectory** *(designed, step 9)*. The session JSONL is
already the trajectory, so the supervisor is just a reader. Free counting heuristics run
first — same tool+args three times, error rate over half, no file written in N turns —
and only a firing heuristic costs a model call.

**Agent-to-agent messaging with no protocol** *(designed, step 10)*. Containers already
share a volume, so a message is a file. No registry, no routing table, no service
discovery. Messages carry paths, not payloads, which is what stops team token cost
growing with N².

**Daemon-first** *(built)*. Detach does not kill the agent. Approvals and questions are
request–response correlated by `execution_id` over the same JSONL framing that carries
everything else.

### Honest comparison

| | Claude Code | Cline / Kilo | **stcode** |
| --- | --- | --- | --- |
| Tool quality (read/edit/grep/bash) | excellent | good | comparable |
| MCP cost | tool defs per turn | tool defs per turn | as code, ~50× cheaper *(step 8)* |
| Runs headless in a container | partial | no | **yes** |
| Multi-agent team across containers | no | no | *step 10* |
| Stagnation detection | no | no | *step 9* |
| Maturity, polish, ecosystem | **far ahead** | ahead | behind, and will stay behind |

We are not competing on polish.

---

## How it fits together

```
cli ──▶ core/daemon ──▶ core/agent ──▶ { core/session, core/harness, core/providers }
core/harness ──▶ core/repl          core/common ◀── everyone (imports nothing back)
```

Dependencies point one way, always.

| | |
| --- | --- |
| `core/providers` | one adapter per SDK, plus `LLMGateway` — difficulty tier → provider + model |
| `core/harness` | the tools, prompts, skills and permissions one agent works with |
| `core/session` | an append-only JSONL transcript: model history, your trace, and the supervisor's input, in one file |
| `core/agent` | the turn loop. `push()` queues, `events()` streams, `interrupt()` stops |
| `core/daemon` | the socket, the session registry, and the approval correlation |
| `cli` | a client, and every user-facing string |

**Approval modes** gate what runs unattended: `plan` reads only, `suggest` asks before
every write and command, `auto-edit` auto-approves edits, `full-auto` asks nothing —
and, without an approver, starts only inside a container. That last rule is code
(`core/daemon/autonomy.py`), not a warning in a document, and there is no override flag.

**Sessions** live at `~/.stcode/sessions/<id>.jsonl`, one append per event, never
rewritten. Branching a session is `cp`.

---

## Development

```bash
uv run pytest                        # the suite
uv run python main.py                # phase 1 smoke test — drives Agent directly
uv run python smoke_daemon.py        # phase 2 smoke test — detach/re-attach, and the guard
```

Both smoke scripts need a reachable model and are meant to be read as much as run: they
are the shortest correct example of driving each layer.

Conventions: Python 3.12+, `uv` for everything, type hints mandatory in `core/`, tests
beside the code as `<package>/_test.py`, commit messages `area: what changed`. Every tool
has a docstring because **the docstring is the prompt the model reads**.

Start with [`CLAUDE.md`](CLAUDE.md) — the working spec, with a status mark against every
component. [`docs/EXPECTED.md`](docs/EXPECTED.md) (Vietnamese) is the architecture
decision it derives from, and wins where the two disagree.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
