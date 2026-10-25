# Sessions

A session keeps a conversation and its work history so you can return to it later.

## Resume a conversation

Open `/sessions` in the terminal, or use the command line:

```bash
stcode sessions
stcode --resume SESSION_ID
```

Use a full ID from the list. If work is still running, you rejoin it. Otherwise,
stcode restores the conversation so you can continue with a new prompt.
Sub-agent conversations are grouped beneath their parent in the session picker.

## Start fresh

Use `/clear` to start a new conversation in the same workspace. Your previous
session stays available. A session is saved once you send a message; opening and
closing an empty conversation does not add an entry to the list.

## Leave and return

`/quit`, **Ctrl+Q**, or closing the terminal disconnects the UI. The background
service keeps running while the machine is on. Actions that need your approval
still require an attached client. Restarting the service or shutting down the
machine stops active work; resume the saved session to continue.

A remote workspace follows the lifecycle of its host or container. Reconnect to
that same service to find its sessions.

## Storage

Sessions are stored under `~/.stcode/sessions` by default. To use another directory:

```toml
[session]
dir = "/path/to/stcode-sessions"
```

The session list belongs to the service you are connected to. In remote mode,
these files are on the remote machine.

## Review usage

Use `/token` to see input, output, and cache token usage for the conversation.
For a visual history of turns, model calls, and tools, enable
[Langfuse or Phoenix tracing](tracing.md).
