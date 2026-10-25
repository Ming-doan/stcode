# Tools

stcode can explore a project, change files, run checks, and use external services.
You describe the task; the agent selects tools and shows their activity in the
transcript. [Approval modes](approval-modes.md) control which actions need your review.

## Built-in capabilities

| Task | Tools |
| --- | --- |
| Find and read code | `ls`, `glob`, `grep`, `read` |
| Create and update files | `write`, `edit` |
| Run commands and collect background output | `bash`, `bash_output` |
| Search and fetch web content | `web_search` |
| Inspect data with Python | `repl` |
| Keep a task plan | `todo_write` |
| Ask you for a decision | `ask_user_question` |
| Load a reusable workflow | `skill` |
| Delegate a focused task | `task` |
| Find and message teammates | `find_teammate`, `send_team_message` in team mode |

For example:

```text
Find where retries are configured, explain the current behavior, and add a test
for a failed request.
```

The available tools depend on your configuration and approval mode.

## Web search

Set `TAVILY_API_KEY` in the environment that starts stcode to enable web search.
You can also reference a key in your configuration:

```toml
[tools]
tavily_api_key = "${TAVILY_API_KEY}"
```

## Choose available tools

To restrict a profile to a chosen set, use `tools`. To remove individual tools
from the default set, use `exclude_tools`:

```toml
[agent]
exclude_tools = ["web_search"]
```

Changes apply when a new agent is created. Use [plan mode](approval-modes.md) for
read-only exploration during a conversation.

## Add capabilities

- Connect an [MCP server](mcp.md) for external tools and services.
- Install a [skill](skills.md) for a reusable procedure.
- Write a [custom Python tool](../sdk/custom-tools.md) for an embedded agent.
