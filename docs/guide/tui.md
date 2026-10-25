# Terminal UI

Run `stcode` inside a project to open the terminal UI. The conversation occupies
the main area, with your prompt and a status line below it. The status line shows
the model, provider, approval mode, and reasoning effort. A working indicator
appears while the agent is responding.

## Write a prompt

| Key | Action |
| --- | --- |
| **Enter** | Send the prompt |
| **Ctrl+J** | Insert a newline |
| **Shift+Enter** or **Alt+Enter** | Insert a newline in terminals that support these keys |
| **Esc** | Interrupt a turn or close a dismissible card |
| **Shift+Tab** | Cycle approval modes |
| **Ctrl+Q** | Leave the UI |

You can send a follow-up while work is in progress. The agent receives it as it
continues. Interrupt first if you want the current work to stop.

## Mention a file

Type `@` at the start of a prompt or after a space. Continue typing to filter
workspace paths, use the arrow keys to select a file, and press **Enter** to insert
its path.

```text
Explain @src/api.py and suggest how to test it.
```

Unicode filenames keep their original characters, including accented letters and
non-Latin scripts. In a Git project, discovery includes tracked and untracked files
while respecting Git ignore patterns. With a remote connection, the list comes
from the remote workspace.

A mention inserts a path into your request; it does not upload a local file to a
remote machine. To work on an image, this release currently supports text and file
paths as input, rather than image attachments.

## Commands

Type `/` to browse and filter commands. Available skills also appear in this list.

| Command | Use it to |
| --- | --- |
| `/model` | Configure providers, credentials, models, and routing |
| `/effort` | Choose reasoning effort supported by your provider |
| `/mode` | Choose an approval mode |
| `/theme` | Select `auto`, `dark`, or `light` |
| `/token` | Review the current session's model usage |
| `/sessions` | Resume a saved conversation |
| `/mcp` | Inspect connected MCP servers |
| `/skills` | Browse discovered skills |
| `/clear` | Start a fresh conversation |
| `/help` | Open the help card |
| `/quit` | Leave the terminal UI |
| `/connect` | Change connections when running with `--daemonless` |

Some commands accept a choice directly, for example `/mode plan`, `/effort high`,
and `/theme dark`. Type `?` in an empty prompt for shortcuts and active file paths.

## Approvals and questions

An approval appears in a card above the prompt. Read the proposed operation and
press **y** to approve or **n** to deny. Approval cards remain open until answered.

When the agent asks a question, select an option with the arrow keys and **Enter**,
or type your own response. See [Approval modes](approval-modes.md) for the available
permission settings.

## Read the transcript

Your messages are labeled **you**. Assistant responses stream as plain text.
Tool calls appear in shaded blocks with a colored left border, their arguments,
and a short result. Failures are labeled, and progress appears while commands run.
When a sub-agent contributes, its name appears beside its messages.

## Run a command yourself

Start a prompt with `!` to run a shell command in the connected workspace:

```text
!git status --short
```

This is a command you execute directly. Its output appears in the terminal but is
not added to the agent's conversation. With a remote connection, the command runs
on the remote machine. Paste relevant output into a prompt if you want the agent
to use it.

## Sessions

`/sessions` opens saved conversations. Child tasks appear beneath their parent;
select the parent to resume it. `/clear` starts a new conversation and keeps the
previous one available in the list.

See [Sessions](sessions.md) for command-line resume and storage settings.
