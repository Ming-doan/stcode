# Sessions

`core/session/`. One append-only JSONL file, three readers.

```py
s = Session.create(cwd=root, role="backend-dev", directory=config.session.dir)
s = Session.create(cwd=root, defer=True)             # no file until the first append
s = Session.resume("01M2QF…")
Session.list(limit=20)

s.append(type="user", content="Add rate limiting")   # sync, ~20µs
s.messages()        # -> list[Message] for the gateway
s.tail(30)          # -> raw records for the supervisor
s.records()         # -> everything, for anyone reading the trajectory
s.meta()            # -> the first meta record: what this session started as
s.overrides()       # -> later meta records, merged: what has been changed since
```

`~/.stcode/sessions/<id>.jsonl`, or `./.stcode/sessions/` with `[session] dir` for
per-project history.

## Append-only, and that is load-bearing

No rewriting, no branch, no fork, no leaf pointer. `cp session.jsonl` is the branching
feature.

It is also what makes `append()` one `write` + `flush` and therefore safe to call
synchronously from the agent loop — the one deliberate exception to "no blocking I/O in
the loop", because ~20µs of buffered write beats the machinery to avoid it.

Opening a file adopts whatever is already in it. Appending to a file whose earlier
content was never loaded breaks the premise: `messages()` would describe a shorter
conversation than the file does. Pointing at an existing file *means* resuming it.

## The file appears on the first message

`Session.create(defer=True)` mints the id and holds the `meta` record in memory. The
file is created on the first `append` — meta first, then that record — and `started`
says which side of that line a session is on.

Two things depend on it. The TUI's `/clear` is "end this session, begin another", and
without deferral that is a directory filling with empty files. And a `stcode` that was
started and closed without a word — wrong directory, checking a flag — used to leave a
transcript that `stcode sessions` listed forever.

The daemon drops an unstarted session with no watchers when the last client detaches: a
session that wrote nothing and has nobody watching is not a session.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

## The record types

```jsonl
{"ts":"…","type":"meta","id":"01M2…","cwd":"/w","role":"backend-dev","model":"claude-opus-5"}
{"ts":"…","type":"user","content":"Add rate limiting to the API"}
{"ts":"…","type":"assistant","content":"Let me look at the middleware first."}
{"ts":"…","type":"tool_call","id":"c1","name":"grep","arguments":{"pattern":"middleware"}}
{"ts":"…","type":"tool_result","id":"c1","name":"grep","content":"src/api/mw.py:12: …","is_error":false}
{"ts":"…","type":"usage","difficulty":"high","stop_reason":"tool_use","input_tokens":12043,"output_tokens":881,"trace_id":"4bf9…"}
{"ts":"…","type":"supervisor","content":"You have grepped 'middleware' 4×. Read mw.py."}
{"ts":"…","type":"inbox","from":"ba","subject":"spec v2","refs":["/team/knowledge/spec.md"]}
{"ts":"…","type":"error","message":"APIConnectionError: …"}
```

`MODEL_VISIBLE` decides which of these reach the model: `user`, `assistant`, `tool_call`,
`tool_result`, `supervisor`, `inbox`. `meta`, `usage` and `error` are for the human
reading the trajectory and for the supervisor — sending the model its own token counts is
paying to tell it something it cannot act on.

**Every event goes in, including the ones the model never sees.** Agent → tool →
sub-agent leaves no natural stack trace, so this file *is* the stack trace.

## `messages()` — three foldings the wire format demands

* an `assistant` record plus the `tool_call`s after it become **one** assistant message
  carrying `ToolUseBlock`s — providers reject a tool call not attached to the assistant
  turn that made it;
* consecutive `tool_result`s become **one** user message of `ToolResultBlock`s, because a
  turn's parallel calls are answered together;
* `supervisor` and `inbox` become **prefixed** user messages — neither is a role any
  provider knows, and the prefix is what stops a nudge reading like a new instruction
  from the user.

## `meta` is a merged view

`meta()` is the **first** `meta` record — what the session started as, and what
`stcode sessions` lists. `overrides()` is every `meta` record after it, merged, later
wins.

That second half is how `/model` and `/effort` change a running session without
breaking rule 4. Nothing is rewritten; a new `meta` record is appended, and the agent
reads `overrides()` before every model call:

```jsonl
{"ts":"…","type":"meta","id":"01M2…","cwd":"/w","model":"claude-sonnet-5"}
{"ts":"…","type":"user","content":"why is this test flaky?"}
{"ts":"…","type":"meta","model":"claude-opus-5","reasoning_effort":"high"}
```

The transcript then says *when* the model changed, which is the only thing that makes a
session that used two models readable afterwards.

Three fields are honoured: `model`, `provider`, `reasoning_effort`. An override with a
`model` but no `provider` keeps the difficulty tier's provider and credentials and swaps
only the model name — that is the gateway's existing rule, not a special case here.

An override chosen **before the first message** has no second record to live in: the
session has no file yet, so `set_meta` folds it into the pending first one rather than
writing a file whose first two lines are both `meta`. It is still an override, and
`overrides()` still returns it. Reading it out of the records alone would lose it, and
what that looks like from outside is `/effort` chosen one message too early applying to
no model call at all.

`[defaults] reasoning_effort` is the layer under all of this: the agent folds it in
beneath whatever the session has overridden, so the config's answer applies from the
first call and a `/effort` during the conversation replaces it.

**Sub-agents do not inherit overrides.** `child()` copies from `meta()` alone, so
`task(difficulty="low")` still routes to the cheap tier. An override that silently
upgraded every scout to the expensive model would make difficulty tiers decorative.

## Ids

`new_id()`, from `core/common/ids.py`. A monotonic ULID: 48 bits of millisecond
timestamp, 80 bits of randomness, Crockford base32, 26 characters, no dashes.

In `core/common/` rather than here because sessions are no longer the only caller — a
team message is a file in an inbox, and it wants the same "sorts by time, needs no
index" property for the same reason.

Sortable by creation time as a plain string — which is the whole reason not to use
`uuid4`. `Session.list()` is then a directory listing: no index, no metadata read, no
SQLite. The newest N sessions are the last N filenames.

Monotonic because "same millisecond" is not hypothetical — spawning three sub-agents in
one turn does it, and random suffixes would order them arbitrarily. Within a millisecond
the random half increments, which also covers NTP stepping the clock backwards.

`uuid.uuid7()` would give the sortability but is stdlib only from Python 3.14, and the
project targets 3.12; a dependency to delete fifteen tested lines that also buy
in-millisecond ordering and a dash-free filename is not a trade worth making yet.

## Sub-agent sessions

`session.child(name)` starts a **separate file**, linked back by one `parent` field in
its `meta`. Interleaving a child's tool calls into the parent's transcript would make
`messages()` produce a conversation neither agent had; one field is enough to reassemble
the tree afterwards.

`agent_name` is the other field, and the two together are what the TUI's `/sessions`
tree is built from — a group-by over the `meta` lines, not an index.

A child is created deferred like any other session, so a sub-agent that is spawned and
fails before its first append leaves no file. Watching a sub-agent's events in the
parent's transcript changes nothing about either file: the events travel on the
`progress` side-channel, and the child's records go only to the child's file.

## Reading a session

```sh
stcode sessions                 # recent sessions, newest first
stcode --resume 01M2QF…         # attach to one, live or from disk
jq -c 'select(.type=="tool_call") | {name, arguments}' ~/.stcode/sessions/<id>.jsonl
jq -s 'map(select(.type=="usage")) | map(.input_tokens + .output_tokens) | add' \
   ~/.stcode/sessions/<id>.jsonl
```

A truncated final line — the process died mid-write — is skipped rather than raised on: a
session you cannot open is one whose trajectory you cannot read, which is exactly when
you most need to.

`prune(keep)` deletes all but the newest N files. `[session] keep` is the number.

## Swapping the store later

A live database replaces this one class; `Agent` still sees the same four methods. Do not
write an abstraction for that now — a class with four methods **is** the abstraction.
