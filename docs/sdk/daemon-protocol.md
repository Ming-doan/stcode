# The daemon protocol

JSONL over a socket — one message per line, UTF-8, flushed. The same framing on unix
and TCP, so a third transport later is an adapter rather than a protocol change.

Everything crossing the process boundary is shaped in `core/daemon/protocol.py` and
nowhere else.

## Using the client

```python
from stcode.core.configs import load_config
from stcode.core.daemon import DaemonClient

config = load_config()
async with await DaemonClient.connect(config) as client:
    session = await client.create(cwd="/workspace", role="backend-dev")
    await client.push("Add rate limiting to /v1/search")

    async for frame in client.events():
        if frame["type"] == "text_delta":
            print(frame["text"], end="", flush=True)
        elif frame["type"] == "turn_finished":
            break
```

## Client → daemon

| Message | Fields | Notes |
| --- | --- | --- |
| `create` | `cwd`, `role`, `approval_mode` | starts a session and attaches this client |
| `attach` | `session`, `replay` | an id the daemon is not holding is resumed from disk |
| `detach` | `session` | **does not stop the agent** |
| `sessions` | `limit` | what this daemon holds, merged over what is on disk |
| `push` | `text`, `session` | queues; mid-turn it waits for the next tool boundary |
| `interrupt` | `session` | stops the turn at its next checkpoint |
| `approval` | `execution_id`, `approved` | answers an `approval_request` |
| `answer` | `execution_id`, `text` | answers a `question` |
| `set_mode` | `mode` | reaches the **live** session, not just later ones |

`session` is optional on everything. A connection remembers the last session it created
or attached to. The field exists because one connection may attach to several at once
and then has to say which it means.

## Daemon → client

| Message | When |
| --- | --- |
| `session` | answer to `create` and `attach`: id, cwd, role, model, mode, busy |
| `sessions` | answer to `sessions` |
| `history` | the transcript so far, sent on attach before the live stream is joined |
| `approval_request` | a tool is waiting on a human |
| `question` | `ask_user_question` is waiting |
| `progress` | a line from a long-running tool. Advisory; nothing is recorded |
| `error` | a protocol- or daemon-level failure |

Plus every [agent event](embedding.md#the-events), **not re-wrapped** — a `TextDelta`
goes on the wire as itself with a `session` field added. A parallel set of wire types
would be a translation layer whose only job is staying in sync.

## Request–response over a stream

Approval is the interesting part. `on_approval` is `async (ApprovalRequest) -> bool`,
which over a socket becomes:

1. broadcast `approval_request` to every attached client,
2. park a `Future` under the `execution_id`,
3. resolve it when any client answers.

The harness needed no change for this: `ApprovalRequest` already carried the id.
Questions use the same mechanism, with the id minted by the daemon because `Question`
has no field for one.

Two consequences:

- **Whoever answers first decides.** A second client on the same session can answer a
  request the first one raised.
- **Losing the last watcher fails every pending request** with a denial explaining why.
  Otherwise a turn blocked on approval whose last client dropped would wait forever on
  a future nobody can resolve.

## Attach is lossless

Subscribe **first**, then replay, then join the live stream. Events arriving during the
replay queue behind it instead of falling into a gap. A record may appear twice — a
duplicate is recoverable, a hole is not.

Anything the agent is currently parked on is re-sent too, so a client attaching
mid-question can answer rather than watch a session that looks hung.

## Speaking it directly

```bash
echo '{"type":"create","cwd":"/workspace"}' | nc -U ~/.stcode/daemon.sock
```

A malformed line produces a diagnosis naming the `type` you sent, not a traceback and
not a dropped connection:

```json
{"type":"error","message":"bad 'push' message: text: Field required"}
```

→ [The daemon (architecture)](../architecture/daemon.md)
