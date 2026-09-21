# Quickstart

```bash
uv run stcode              # work in this directory
uv run stcode ~/code/api   # or in that one
```

The first run opens a one-time setup screen. Pick a provider, paste an API key — or
leave it blank if you exported one — choose a model, and you are in.

## Your first task

Type a request and press enter. Nothing about the phrasing is special; there is no
command language.

```
Add rate limiting to the /v1/search endpoint.
```

What happens next, in order:

1. The agent reads the repository's own instructions if it has any — `CLAUDE.md`,
   `AGENTS.md`, or `.stcode/instructions.md`, first hit wins.
2. It looks around with `grep`, `glob` and `read` before changing anything.
3. The first time it wants to write a file or run a command, **you are asked**. The
   default mode is `suggest`, which asks every time.
4. It verifies — usually by running your tests — and then says what it did.

Press ++shift+tab++ to cycle the approval mode if you get tired of being asked, or
++esc++ to stop the turn in flight.

## While it is working

You do not have to wait for it to finish before typing. A message sent mid-turn is
delivered at the next **tool-call boundary** — after the current round of results,
before the next request to the model. That is what makes steering possible: your
correction changes what it is about to do, instead of being lost or corrupting the
conversation.

```
Actually, use a token bucket, not a fixed window.
```

## Picking up where you left off

Closing the terminal does not stop the agent — the work is in a daemon.

```bash
uv run stcode sessions        # recent sessions, newest first
uv run stcode --resume 01HX…  # attach to one, live or from disk
```

If the session is still running in the daemon, you join it live. If it finished, the
transcript is read back off disk and you continue it. Same command either way. →
[Sessions](../guide/sessions.md)

## Where to next

- The agent asks too much, or too little → [Approval modes](../guide/approval-modes.md)
- You want it in a container → [The three shapes](../guide/shapes.md)
- You want it to know about your internal tools → [MCP servers](../guide/mcp.md)
- You want a repeatable procedure it can load → [Skills](../guide/skills.md)
