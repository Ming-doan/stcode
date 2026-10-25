# Approval modes

Choose how much the agent can do without asking you. The default is `suggest`.

| Mode | Read files | Edit files | Run commands or Python | Web access |
| --- | --- | --- | --- | --- |
| `plan` | Allowed | Unavailable | Unavailable | Asks |
| `suggest` | Allowed | Asks | Asks | Asks |
| `auto-edit` | Allowed | Allowed | Asks | Allowed |
| `full-auto` | Allowed | Allowed | Allowed | Allowed |

Use `plan` to explore a change, `suggest` to review proposed actions, or `auto-edit`
when you want file edits to proceed while you approve execution. MCP calls follow
the execution setting, including tools offered by remote servers.

## Change modes

Use `/mode` to choose, or set a mode directly:

```text
/mode plan
```

**Shift+Tab** cycles the available modes. You can also open a session with a mode:

```bash
stcode --mode auto-edit
```

The change applies to subsequent operations. It does not cancel an operation
already running; press **Esc** to interrupt the current turn.

## Answer an approval

Review the operation shown above the prompt. Press **y** to approve or **n** to
deny. If you deny an action, you can send a follow-up explaining how to proceed.

## Unattended work

The CLI requires a container environment for `full-auto`. Use the provided Docker
image for unattended work; see [Local and remote work](shapes.md).

A session using an approval-based mode needs an attached client to approve actions.
Leaving the terminal does not grant permission to execute pending operations.
