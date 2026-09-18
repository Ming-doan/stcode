# The TUI

A [textual](https://textual.textualize.io/) app that is, architecturally, just a client
of the daemon. Everything it can do is a protocol message.

## Keys

| | |
| --- | --- |
| ++enter++ | send |
| ++shift+tab++ | cycle approval mode — takes effect on the **live** session |
| ++esc++ | interrupt the turn in flight |
| ++f2++ | provider, key and model settings |

## Slash commands

| | |
| --- | --- |
| `/model` | provider, key, model — the same screen as ++f2++ |
| `/mode` | cycle the approval mode |
| `/mode <name>` | set it directly: `plan`, `suggest`, `auto-edit`, `full-auto` |
| `/connect` | point this client at a different daemon |
| `/clear` | clear the transcript **on screen** — the session file is untouched |
| `/help` | the list above |
| `/quit`, `/exit`, `/q` | leave. The agent keeps working. |

!!! note "`/clear` is a display command"

    It clears what you are looking at, not what the model remembers. Sessions are
    append-only and are never rewritten — that is load-bearing, not an oversight. To
    start fresh, start a new session.

## Approvals and questions

When a tool needs permission, a modal appears with the tool name and its arguments.
Approving or denying sends an `approval` message carrying the `execution_id` the daemon
minted, and the tool — which has been parked on a future this whole time — resumes.

Two consequences worth knowing:

- **Whoever answers first decides.** If two terminals are watching, either can approve.
- **A denial is a denial, not a failure.** The tool is told the user declined, and the
  model is expected to adapt rather than retry.

If the agent asks you a *question* (via `ask_user_question`) you get up to four
options. Four, because it fits a modal, maps to number keys, and forces the agent to
prune its own list rather than hand you twelve.

## Streaming and steering

Text streams as the model produces it. You can type while it works — a message sent
mid-turn is delivered at the next tool-call boundary, never spliced into the model call
in flight. → [The agent loop](../architecture/agent-loop.md)

## What the status line tells you

The current approval mode, the model in use, and the daemon address. If the model is
empty, nothing has been chosen yet and the chat screen says so rather than silently
guessing one.
