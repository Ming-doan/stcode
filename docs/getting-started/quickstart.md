# Quickstart

## Install and open a project

```bash
uv tool install stcode
cd your-project
stcode
```

You can also open a directory directly with `stcode ~/projects/api`.

## Connect a model

On your first visit, confirm that you trust the project directory. If no provider
credential is configured, stcode opens the setup screen. Choose OpenAI, Anthropic,
or Google, add your API key, and choose a model available to your account.

You can export a key before launching instead:

```bash
export OPENAI_API_KEY="your-api-key"
stcode
```

For Anthropic use `ANTHROPIC_API_KEY`; for Google use `GEMINI_API_KEY`.
Use `/model` to select the provider, key, model, and routing at any time.
See [Configuration](configuration.md) for compatible endpoints and multiple accounts.

## Try a first task

Start with a question about the project:

```text
Explain how requests reach the API handlers. Point me to the relevant files.
```

Then give the agent a scoped change:

```text
Add input validation to the search endpoint and run the relevant tests.
```

Type `@` to find a file and include its path in your request. The transcript shows
the agent's replies, tool calls, and results as it works.

The default `suggest` mode asks you before edits and command execution. Respond to
an approval card with **y** to approve or **n** to deny. Use `/mode plan` when you
want to explore an approach before making changes.

## Steer the work

Send a follow-up while the agent is working:

```text
Keep the existing response format so callers do not need to change.
```

Press **Esc** to interrupt the turn. Use **Ctrl+J** to add a newline to your prompt
and **Enter** to send it. Type `?` in an empty prompt for help.

## Return to a conversation

Use `/sessions` inside the terminal, or list sessions from your shell:

```bash
stcode sessions
stcode --resume SESSION_ID
```

Replace `SESSION_ID` with an ID from the list. Leaving the UI keeps the local
background service running; work that needs an approval still needs an attached
client. A machine shutdown or service restart stops active work, but saved sessions
can be resumed.

Next, explore the [terminal UI](../guide/tui.md),
[approval modes](../guide/approval-modes.md), or [MCP servers](../guide/mcp.md).
