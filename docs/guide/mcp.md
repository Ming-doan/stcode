# MCP servers

Connect MCP servers to give stcode access to documentation, services, and tools
beyond the project files. Use `/mcp` to see which servers the current session has
connected to.

## Add a server

Create `.mcp.json` in your project. This example connects to a local server over
stdio and a hosted server over HTTP; replace the command and URL with your server's
connection details:

```json
{
  "mcpServers": {
    "local-tools": {
      "command": "python",
      "args": ["/absolute/path/to/your_mcp_server.py"]
    },
    "docs": {
      "url": "https://your-server.example/mcp"
    }
  }
}
```

Install any runtime the server needs in the environment where stcode runs. For a
remote workspace, configure the server on that machine. Start a new session to
load the configuration, then ask for a task that uses the server:

```text
Use the documentation server to check how this library handles retries.
```

## Share servers across projects

Put global entries in `~/.stcode/.mcp.json` or `~/.stcode/mcp.json`. Project entries
come from `.mcp.json` or `mcp.json` in the workspace. `STCODE_MCP_CONFIG` can name an
additional JSON file.

Files are combined in that order. If a server name occurs more than once, its
later entry replaces the earlier entry. Within each location, `mcp.json` follows
`.mcp.json`.

## Authentication

A hosted server can use request headers:

```json
{
  "mcpServers": {
    "company-tools": {
      "url": "https://your-server.example/mcp",
      "headers": {"Authorization": "Bearer your-server-token"}
    }
  }
}
```

Keep configurations containing credentials outside version control. Local servers
inherit stcode's environment; an `env` object on the server entry can set
server-specific values.

## How the agent uses tools

By default, stcode lets the agent discover and call MCP tools through Python.
You can choose direct tool exposure instead:

```toml
[mcp]
expose = "tools"
```

The default is `expose = "code"`. Both modes use the same server configuration.
MCP calls follow your [execution approval setting](approval-modes.md).

## Disable a server

Set `"enabled": false` on its JSON entry, or disable MCP for new sessions in
`config.toml`:

```toml
[mcp]
enabled = false
```

If a connection fails, check the server's command, credentials, and reachability
from the workspace host. Other available tools remain usable.
