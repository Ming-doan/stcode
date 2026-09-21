# MCP servers as code

`core/harness/mcp.py`. We do not reimplement MCP — the official `mcp` SDK owns the
protocol, and this is the adapter to `harness/tools/base.py`.

Config is the familiar `mcpServers` object, so an existing `.mcp.json` works unchanged:

```json
{"mcpServers": {
  "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
  "docs": {"type": "http", "url": "https://example.com/mcp"}
}}
```

## The bet

Other agents ship MCP schemas in the prompt prefix every turn — 10–30k tokens for three
servers, on every request, forever. Anthropic measured the code-instead-of-definitions
pattern at 150k → 2k tokens.

Measured here on three servers and twelve tools: **9,869 → 0 characters per turn**
(`smoke_mcp.py`). Not "fewer" — zero, because nothing about those servers is in the
prompt at all.

## `expose = "code"` — the default

Nothing is registered as a tool. Each server's tools are written out as Python:

```
.stcode/mcp_servers/
  github/
    __init__.py
    create_issue.py
    search_code.py
  fs/
    read_file.py
```

The agent greps for what it needs, reads the one file, and calls it from `repl`. The
generated stub opens **its own connection** inside the REPL process, so there is no
reverse-RPC bridge back into the daemon and nothing to keep alive between calls.

The prompt gets one short section naming the directory and the servers — not their tools,
not their schemas. Discovery costs a `grep`; the result stays in a REPL variable instead
of the context window.

The workspace and `.stcode/` are both on the REPL's `PYTHONPATH`, which is what makes
`from mcp_servers.github import create_issue` work from a cell.

One tree, in the workspace, generated from the **merged** config — so a global server
and a project one are siblings under the same package and the agent has one directory to
grep, not two. It is deleted and rewritten each session and carries a `.gitignore` of
its own (`*`), because it is a build artefact and asking every project to add a line to
its own ignore file is asking for the one that forgets.

### One loop, one connection

Each stub calls `mcp.call(server, tool, …)`, which connects on first use and caches the
manager **with the event loop it was opened on**. The loop is part of the key because a
connection does not outlive it: a cell that runs `asyncio.run(...)` gets a loop of its
own, and closing it cancels the task holding the `AsyncExitStack`, whose `finally`
empties the manager's clients.

Cached by name alone, the next call got that manager back and indexed an empty dict —
a bare `KeyError: '<server>'` raised three frames below anything that could explain it,
with no way to recover short of restarting the REPL. Keyed by loop, a stale entry is
dropped and the next call reconnects. The prompt and `repl`'s docstring also say to use
top-level `await`, which is the case where nothing is dropped in the first place.

## `expose = "tools"` — the escape hatch

Registers them the ordinary way and advertises them every turn. One small server with two
tools is genuinely cheaper advertised directly than discovered over three REPL
round-trips. The token argument is real but not universal.

In this mode the manager's connections are held open for the session. In `code` mode the
connection is opened only to ask what each server offers, then closed — holding it would
mean two connections per server, one of them unused.

## Three rules at the boundary

* **Schemas pass through untouched.** The server's own description is authoritative;
  validating against a model we invented would reject calls it would have accepted.
* **Names are namespaced** `mcp__<server>__<tool>`. Two servers offering `search` is
  normal, and a silent shadow is very hard to notice from the outside. The registry
  refuses to replace an existing name unless explicitly asked — an MCP server taking over
  `read` would redirect every file read in the session with nothing in the log to say so.
* **Every MCP tool is `EXECUTE`.** We cannot see what a server does: the name says
  "search" and the code may write files. The permission reflects what is known, not what
  is claimed.

## Failure is soft

A server that will not start, or times out connecting (30s), is reported and skipped.
Neither is a reason a coding session cannot begin — the agent has fifteen other tools and
the one it cannot reach is named in the log.

## Configuration

```toml
[mcp]
enabled = true
expose  = "code"        # or "tools"
```

Servers come from every config that applies, merged by name, lowest precedence first:
`~/.stcode/{mcp,.mcp}.json`, then the workspace's `{.mcp,mcp}.json`, then
`$STCODE_MCP_CONFIG`, then an explicit path. A name defined twice takes the later
entry **whole** — a field-level merge would produce a server neither file describes.

Merged rather than first-wins because the two files answer different questions: the
global one is "the servers I use everywhere", the project one is "the servers this
repository needs", and letting either hide the other makes adding one server look like
losing all the rest.

`url` servers accept `headers`, which is how a hosted server is authenticated. The SDK
builds its own HTTP client for a bare URL, so a configured header means building the
transport here instead — the alternative is accepting an API key and never sending it.
