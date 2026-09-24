# stcode

A coding agent for the terminal, built the other way round: **the agent lives in a
daemon, and the UI is a client of it.**

Closing the window does not stop the work. Several terminals can watch the same session.
Moving the agent into a container is a config key — `transport = "tcp"` — not a rewrite.
And because a container is a real boundary, `full-auto` can exist without being a
foot-gun: the daemon refuses to start in that mode anywhere its blast radius is not
contained.

---

## Quickstart

```bash
uv sync                      # Python 3.10+
uv run stcode                # first run opens a one-time setup screen
```

Full documentation: `uv run --group docs mkdocs serve`, or read
[`docs/`](docs/index.md).

Pick a provider, paste an API key (or leave it blank and export `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `GEMINI_API_KEY`), choose a model, and type.

### The three shapes

```bash
uv run stcode                # UI + a daemon, started here if none is listening
uv run stcode --headless     # the daemon alone — what a container runs
uv run stcode --daemonless   # the UI alone, attached to a daemon elsewhere
```

`--daemonless` never starts an agent locally: if nothing answers, it asks *which daemon?*
rather than quietly running the work on your laptop. `/connect` moves a running UI to a
different one.

### Other commands

```bash
uv run stcode sessions       # recent sessions, newest first
uv run stcode --resume 01HX… # attach to one, live or from disk
uv run stcode config         # where config lives and what it selects
```

Useful flags: `--cwd`, `--mode` (`plan` | `suggest` | `auto-edit` | `full-auto`),
`--model`, `--role` (which agent profile supplies the prompt), `--team` (team mode, off
by default), `--config`, and `--transport` / `--socket` / `--host` / `--port` to point at
a daemon for this run without editing the config file.

### In the UI

| | |
| --- | --- |
| `shift+tab` | cycle approval mode — takes effect on the live session |
| `esc` | interrupt the turn in flight |
| `f2` / `/model` | provider, key, model |
| `/connect` | point this client at a different daemon |
| `/mode`, `/clear`, `/help`, `/quit` | |

### Attaching to an agent in a container

```bash
# in the container
docker run -e STCODE_SANDBOX=1 -e ANTHROPIC_API_KEY -p 7717:7717 \
    -v "$PWD:/workspace" stcode --headless --transport tcp --host 0.0.0.0

# on your machine
uv run stcode --daemonless --transport tcp --host <container-ip> --port 7717
```

---

## What it does differently

Four things, each traceable to a measurement. The full argument, with the numbers, is
[`CLAUDE.md`](CLAUDE.md) §2.

**MCP tools as code, not tool definitions.** Most agents ship MCP schemas in the prompt
prefix every turn: 10–30k tokens, forever. stcode writes them to
`.stcode/mcp_servers/<server>/<tool>.py` and lets the agent `grep` and import what it
needs. Measured on three servers and twelve tools: **9,869 → 0 chars per turn.**

**A supervisor that watches the trajectory.** The session JSONL is already the trajectory,
so the supervisor is just a reader. Free counting heuristics run first — the same call
three times, over half the calls failing, no file written in N turns — and only a firing
heuristic costs a model call.

**Agent-to-agent messaging with no protocol.** Containers share a volume, so a message is
a file. No registry, no routing table, no service discovery. Messages carry paths, not
payloads, which stops team token cost growing with N².

**Daemon-first.** Detach does not kill the agent. Approvals and questions are
request–response correlated by `execution_id` over the same JSONL framing as everything
else.

### Honest comparison

| | Claude Code | Cline / Kilo | **stcode** |
| --- | --- | --- | --- |
| Tool quality (read/edit/grep/bash) | excellent | good | comparable |
| MCP cost | tool defs per turn | tool defs per turn | as code, 9.9k → 0 chars/turn |
| Runs headless in a container | partial | no | **yes** |
| Multi-agent team across containers | no | no | **yes** |
| Stagnation detection | no | no | **yes** |
| Maturity, polish, ecosystem | **far ahead** | ahead | behind, and will stay behind |

We are not competing on polish.

---

## How it fits together

```
cli ──▶ core/daemon ──▶ core/agent ──▶ { core/session, core/harness, core/providers }
                                   └──▶ core/team (team mode only)
core/harness ──▶ core/repl          core/common ◀── everyone (imports nothing back)
```

Dependencies point one way, always. [`CLAUDE.md`](CLAUDE.md) §3 has the contract for each
component.

**Approval modes** gate what runs unattended: `plan` reads only, `suggest` asks before
every write and command, `auto-edit` auto-approves edits, `full-auto` asks nothing — and,
without an approver, starts only inside a container. That last rule is code
(`core/daemon/autonomy.py`), not a warning in a document, and there is no override flag.

**Sessions** live at `~/.stcode/sessions/<id>.jsonl`, one append per event, never
rewritten. Branching a session is `cp`.

---

## Status

All five phases are built: the turn loop, tools, transcript, daemon and TUI client; the
REPL and MCP-as-code; the stagnation supervisor; multi-container team mode. Each phase has
a smoke script proving its gate against real processes:

```bash
uv run python smoke_repl.py         # the REPL, and tool_out
uv run python smoke_mcp.py          # three MCP servers, prompt prefix unchanged
uv run python smoke_supervisor.py   # a loop caught and redirected
uv run python smoke_daemon.py       # detach/re-attach, and the autonomy guard
uv run python smoke_team.py         # two containers, one volume (needs Docker)
```

They need a reachable model and are meant to be read as much as run — each is the
shortest correct example of driving its layer.

**What is not done:** the ~20 team-mode evaluation tasks in
[`docs/architecture/team.md`](docs/architecture/team.md#the-evaluation-set) are written
and have not been run. Until they are, whether team mode helps or merely spends 15× the
tokens is an open question. [`CLAUDE.md`](CLAUDE.md) §7 explains why that is the honest
thing to say about it.

---

## Development

```bash
uv run pytest
```

```bash
uv run pytest -m live                     # the two tests that call a real provider
uv run --group docs mkdocs serve          # the documentation site
```

Conventions: Python 3.10+, `uv` for everything, type hints mandatory in `core/`, tests
in `tests/`, commit messages `area: what changed`. Every tool has a docstring because
**the docstring is the prompt the model reads**.

**Before writing code**, read
[how a change is made](docs/contributing/workflow.md) — spec → question → document →
test → implement → run → commit. The document comes before the test on purpose: a
feature whose page is hard to write is a feature whose shape is wrong.

Start with [`CLAUDE.md`](CLAUDE.md) — what the project is, what it bets on, and the
seven rules — then [`docs/architecture/`](docs/architecture/index.md) for the component
you are about to change, and [`docs/decisions/`](docs/decisions/index.md) for the
questions already settled.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).
