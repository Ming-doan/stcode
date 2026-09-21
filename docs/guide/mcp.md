# MCP servers

stcode speaks MCP through the official SDK — the protocol is not reimplemented. What
*is* different is how a connected server reaches the model.

## Configuration

The familiar `mcpServers` object, so an existing `.mcp.json` works unchanged:

```json
{
  "mcpServers": {
    "fs":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "docs": {"type": "http", "url": "https://example.com/mcp"}
  }
}
```

### Where the config is read from

**Every one of these that exists, merged**, lowest precedence first:

| | |
| --- | --- |
| `~/.stcode/mcp.json` or `~/.stcode/.mcp.json` | global — the servers you want in every project |
| `.mcp.json` or `mcp.json` in the workspace | the project's, and the one you check in |
| `$STCODE_MCP_CONFIG` | a file named by the environment |
| `--config`'s `[mcp] config` | an explicit path |

Merged, not first-wins: adding one server to a project must not silently unplug every
global one. When the same **name** appears twice, the later file's entry wins *whole* —
there is no field-level merge, because half a command from one file and half from
another is a server nobody configured. (This is what Claude Code does too, with three
scopes instead of two.)

A missing config is normal, not an error.

### Authenticating a hosted server

A `url` server is usually authenticated by a header:

```json
{"mcpServers": {
  "context7": {
    "type": "http",
    "url": "https://mcp.context7.com/mcp",
    "headers": {"CONTEXT7_API_KEY": "ctx7sk-…"}
  }
}}
```

`headers` is sent with every request to that server. Put a file with a key in it in
`~/.stcode/`, not in a repository.

## Two ways to expose a server

```toml
[mcp]
expose = "code"   # or "tools"
```

### `code` — the default

**Nothing is registered as a tool.** Each of the server's tools becomes a Python file:

```
.stcode/mcp_servers/fs/read_file.py
.stcode/mcp_servers/fs/list_directory.py
.stcode/mcp_servers/docs/search.py
```

The prompt learns only that the servers exist and where their stubs live. The agent
greps the directory, reads the one file it needs, and calls it from
[`repl`](repl.md) — where the result stays in a variable it can slice.

The tree is written into the **workspace**, once, from the merged config — a global
server and a project one land side by side under the same `mcp_servers/` package, so
there is only ever one place to look. It is regenerated from scratch every session and
carries its own `.gitignore`, because a checked-in stub is a build artefact that is
wrong the moment the server changes.

Calls are awaited at the top level of the cell:

```py
from mcp_servers.context7 import resolve_library_id
found = await resolve_library_id(libraryName="fastapi", query="routing")
```

**Not** `asyncio.run(...)`. The REPL has one event loop for the whole session, and
`asyncio.run` opens a second one and closes it on the way out — which drops the server
connection the next cell would have reused.

### `tools` — the escape hatch

Advertises them the ordinary way, in the prompt prefix, every turn.

## Why `code` is the default

Tool definitions sit in the **cached prefix**: they are sent on every single request,
for the life of the session. Three mid-sized servers is 10–30k tokens per turn,
forever.

Measured here, on three servers and twelve tools:

| | Prompt prefix cost per turn |
| --- | --- |
| `expose = "tools"` | **9,869 chars** |
| `expose = "code"` | **0 chars** |

Anthropic measured the same pattern at 150k → 2k tokens. The saving is real because the
schemas do not vanish — they move to disk, where a `grep` reaches them for the price of
one tool call instead of every turn.

The token argument is real but not universal: **one small server with two tools is
cheaper advertised directly** than discovered over three REPL round-trips. That is what
`tools` mode is for.

## Rules at the boundary

- **Schemas pass through untouched.** The server's own description is authoritative;
  validating against a model we invented would reject calls it would have accepted.
- **Names are namespaced** `mcp__<server>__<tool>`. Two servers offering `search` is
  normal, and a silent shadow is very hard to notice from outside.
- **Every MCP tool is `EXECUTE`.** We cannot see what a server does — the name says
  "search" and the code may write files — so the permission reflects what is *known*,
  not what is claimed.

## Turning it off

```toml
[mcp]
enabled = false
```

A server that is unreachable is reported, not raised on. A broken MCP server is not a
reason a coding session cannot start.

→ [MCP as code (architecture)](../architecture/mcp.md)
