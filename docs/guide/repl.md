# Working with data

stcode can run Python to inspect structured files, analyze command output, and
combine results from external tools. Variables remain available during the
conversation, so the agent can refine an analysis without repeating every step.

## Ask for an analysis

```text
Read report.json, group failures by service, and show the most common causes.
```

```text
Compare the API responses in these two files and list the fields that changed.
```

Python execution follows your [approval mode](approval-modes.md), just like shell
commands. Review execution requests when working in `suggest` or `auto-edit` mode.

## Large results

The transcript shows a preview of large tool results. Ask the agent to narrow the
search, inspect a specific section, or use Python to extract the relevant records.
You do not need to copy a long result back into the conversation.

## External tools

The agent can also use Python to combine calls to [MCP servers](mcp.md), such as
looking up documentation and selecting the results relevant to your project.

## Interrupt and restart

Press **Esc** to interrupt active work. Start a fresh conversation with `/clear`
when you want a new analysis. Python variables are temporary; save important
results to files if you need to keep them across sessions or restarts.
