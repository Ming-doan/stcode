# Tools

What the agent can do. Each one is a Python function whose **signature is the schema**
and whose **docstring is the prompt the model reads**.

## The built-in set

| Tool | Permission | What it does |
| --- | --- | --- |
| `read` | read | a file, with line numbers |
| `write` | write | replace a file entirely |
| `edit` | write | replace an exact, unique string |
| `bash` | execute | one command, in a fresh process |
| `bash_output` | read | drain a backgrounded command |
| `glob` | read | filenames by pattern |
| `grep` | read | content search, via `ripgrep` |
| `ls` | read | a directory |
| `todo_write` | read | keep the plan in the context window |
| `web_search` | read | needs `TAVILY_API_KEY` |
| `ask_user_question` | read | up to four options, put to the human |
| `skill` | read | load a procedure's instructions |
| `repl` | execute | a persistent Python namespace |
| `task` | execute | a sub-agent, added by the agent layer |
| `send_message` | execute | team mode only |

A sub-agent gets a narrower set: no `task` (one that can spawn is one for which
`max_depth` bounds nothing) and no `repl` (a persistent namespace is the parent's job).

## Read before write

`write` and `edit` both **refuse a file this session has not read**, and warn when it
changed on disk underneath them.

The cost is deliberately asymmetric. A refused edit costs one turn. An overwritten file
the agent never read costs work that no longer exists.

## `edit` matches a string, not a line range

```python
edit(path="src/util.py", old_string="RETRY_LIMIT = 3", new_string="RETRY_LIMIT = 5")
```

The match must be **unique** unless `replace_all=True`. Line numbers go stale the
moment anything above them moves, and a fuzzy match silently edits the wrong place,
while a unique literal either matches once or fails loudly.

When it fails, it says *why* — indentation differs, or the first line matched and the
mismatch is further down — because a failure you can act on in one step is worth more
than a correct error message.

!!! tip "The line-number prefix is not part of the file"

    `read` prefixes every line with a number and a tab. Including that prefix in
    `old_string` is the most common reason an edit finds no match.

## Output is elided, never summarised

Every tool has a character cap — 8192 by default, 32768 for `read`, because file
contents are the ground truth an agent reasons from and a half-seen function is worse
than a slow turn.

Over the cap, the **head and tail are kept and the middle is dropped**:

```
… [41,203 chars elided — the whole value is in tool_out["grep_a1b2"] — slice it with `repl`] …
```

The beginning says what ran; the end says how it went. Summarising with a model would
lose information; eliding does not — *provided the whole value is somewhere the model
can reach*. That proviso is the entire rule, so the hint is only allowed to name
`tool_out[...]` when a REPL really holds it. → [The REPL and `tool_out`](repl.md)

## Background commands

`bash` can background a long command and hand back a handle; `bash_output` drains it.
Each `bash` call is a **fresh process** — there is no shell whose state persists
between calls, because stateful shells make every failure irreproducible.

For state that should persist, use `repl`.

## Permissions

A tool declares a `ToolPermission` once — `READ`, `WRITE` or `EXECUTE` — and never
decides its own gating. [Approval modes](approval-modes.md) map that class onto
"allowed", "ask", or "forbidden".

One refinement: a call may *narrow* its class. `cat x.py` and `rm -rf build/` arrive
through the same `bash` tool, so a read-only command is recognised as read-only and
skips the approval a write would need.

## Adding your own

→ [Writing a tool](../sdk/custom-tools.md)
