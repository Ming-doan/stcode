# stcode

A coding agent for the terminal, built the other way round: **the agent lives in a
daemon, and the UI is a client of it.**

Closing the window does not stop the work. Several terminals can watch the same
session. Moving the agent into a container is a config key — `transport = "tcp"` — not
a rewrite. And because a container is a real boundary, `full-auto` can exist without
being a foot-gun: the daemon refuses to start in that mode anywhere its blast radius is
not contained.

```bash
uv tool install stcode
stcode
```

## Where to go

<div class="grid cards" markdown>

- **Just installed it**

    [Installation](getting-started/installation.md) ·
    [Quickstart](getting-started/quickstart.md) ·
    [Configuration](getting-started/configuration.md)

- **Using it day to day**

    [The three shapes](guide/shapes.md) ·
    [The TUI](guide/tui.md) ·
    [Approval modes](guide/approval-modes.md) ·
    [Sessions](guide/sessions.md)

- **Extending it**

    [Tools](guide/tools.md) ·
    [Skills](guide/skills.md) ·
    [MCP servers](guide/mcp.md) ·
    [Writing a tool](sdk/custom-tools.md)

- **Running a team**

    [Setting one up](teams/setup.md) ·
    [Roles](teams/roles.md) ·
    [How a team works](teams/workflow.md)

- **Building on it**

    [Embedding an agent](sdk/embedding.md) ·
    [The daemon protocol](sdk/daemon-protocol.md)

</div>

## What it does differently

Four things, each traceable to a measurement.

**MCP tools as code, not tool definitions.** Most agents ship MCP schemas in the prompt
prefix every turn: 10–30k tokens, forever. stcode writes them to
`.stcode/mcp_servers/<server>/<tool>.py` and lets the agent `grep` and import what it
needs. Measured on three servers and twelve tools: **9,869 → 0 chars per turn.** →
[MCP servers](guide/mcp.md)

**A supervisor that watches the trajectory.** The session JSONL is already the
trajectory, so the supervisor is just a reader. Free counting heuristics run first —
the same call three times, over half the calls failing, no file written in N turns —
and only a firing heuristic costs a model call. →
[The supervisor](architecture/supervisor.md)

**Agent-to-agent messaging with no protocol.** Containers share a volume, so a message
is a file. No registry, no routing table, no service discovery. Messages carry paths,
not payloads, which stops team token cost growing with N². →
[How a team works](teams/workflow.md)

**Daemon-first.** Detach does not kill the agent. Approvals and questions are
request–response correlated by `execution_id` over the same JSONL framing as everything
else. → [The daemon](architecture/daemon.md)

## Honest comparison

| | Claude Code | Cline / Kilo | **stcode** |
| --- | --- | --- | --- |
| Tool quality (read/edit/grep/bash) | excellent | good | comparable |
| MCP cost | tool defs per turn | tool defs per turn | as code, 9.9k → 0 chars/turn |
| Runs headless in a container | partial | no | **yes** |
| Multi-agent team across containers | no | no | **yes** |
| Stagnation detection | no | no | **yes** |
| Maturity, polish, ecosystem | **far ahead** | ahead | behind, and will stay behind |

We are not competing on polish.

!!! warning "One open question"

    The ~20 team-mode evaluation tasks in
    [Team mode](architecture/team.md) are written and **have not been run**. Until they
    are, whether team mode helps or merely spends 15× the tokens is unanswered.
