# Sessions

Every session is one append-only JSONL file.

```
~/.stcode/sessions/01HXZ8Q2K3M4N5P6R7S8T9V0W1.jsonl
```

One line per event — including the events the model never sees. It is the model's
history, the trajectory log and the supervisor's input, all from the same file. There
is no second logger to keep in sync.

## Working with them

```bash
uv run stcode sessions          # recent sessions, newest first
uv run stcode --resume 01HX…    # attach live, or read back from disk
```

The id is a monotonic ULID: millisecond timestamp, then randomness, base32. It sorts by
creation time as a plain string, which is why listing sessions needs no index and no
database — the newest N are the last N filenames.

Keeping them with the project instead of in your home directory:

```toml
[session]
dir = "./.stcode/sessions"
keep = 100
```

## Append-only, and that is load-bearing

Records are never rewritten. No branch pointer, no fork, no leaf.

```bash
cp ~/.stcode/sessions/01HX….jsonl ~/.stcode/sessions/01HY….jsonl
```

**That is the branching feature.** It is also what makes appending one line safe to do
synchronously from the agent loop — the single deliberate exception to "no blocking I/O
in the loop", because ~20µs of buffered write beats the machinery required to avoid it.

## What is in the file

| Record | Seen by the model | Why it exists |
| --- | --- | --- |
| `meta` | no | cwd, role, model — what `sessions` lists |
| `user` | yes | what you asked |
| `assistant` | yes | what it said |
| `tool_call` / `tool_result` | yes | folded into the wire format for the next request |
| `supervisor` | yes | a nudge, rendered as a prefixed user message |
| `inbox` | yes | a teammate's message, in team mode |
| `usage` | **no** | token counts, including cache hits |
| `error` | **no** | a turn that ended badly |

Sending the model its own token counts would be paying to tell it something it cannot
act on — so `usage` stays for you, and for the supervisor.

## Reading a trajectory

Agent → tool → sub-agent leaves no natural stack trace. This file *is* the stack trace,
which is why the events the model never sees are kept.

```bash
jq -r 'select(.type=="tool_call") | "\(.name) \(.arguments)"' session.jsonl
jq -r 'select(.type=="usage") | .input_tokens, .cache_read_input_tokens' session.jsonl
```

A non-zero `cache_read_input_tokens` on the second turn is the evidence that prompt
caching held. That claim is otherwise unverifiable, which is exactly why the number is
written down. → [Sessions (architecture)](../architecture/session.md)

## Cleaning up

`[session] keep` bounds how many are retained; the oldest beyond it are deleted. Since
ULID filenames sort by time, "the newest 100" is a slice of a directory listing.
