# The daemon

`core/daemon/`. **One daemon, many sessions**, addressed by id. A TUI is a client of it.

That inversion — daemon first, UI second — is what makes the container story real rather
than aspirational: moving the agent into a container is a config key, not a rewrite.

```py
async with Daemon(config) as daemon:          # binds; `full-auto` refuses here
    await daemon.serve_forever()

async with await DaemonClient.connect(config) as client:
    await client.create(cwd=Path.cwd())
    await client.push("Add rate limiting")
    async for frame in client.events(): ...
```

Four modules, one job each:

| | |
| --- | --- |
| `protocol.py` | the JSONL message shapes — the only thing on the wire |
| `runner.py` | `SessionRunner`: one live session, its watchers, its parked requests |
| `server.py` | the socket, the registry, one connection object per client |
| `autonomy.py` | `guard_autonomy` / `in_container` — the `full-auto` rule, as code |

## The three shapes

```bash
stcode                # UI + a daemon, started here if none is listening
stcode --headless     # the daemon alone — what a container runs
stcode --daemonless   # the UI alone, attached to a daemon elsewhere
```

No implementation switches between them. `--daemonless` never starts an agent locally: if
nothing answers it asks *which daemon?* rather than quietly running the work on your
laptop. `/connect` moves a running UI to a different one.

### Workspace and CWD ownership

The **daemon owns file execution, tool execution, and session state**.
* In **solo mode** (`stcode`), the TUI and daemon run on the same host, and the TUI supplies
  its local directory to seed the session's `cwd`.
* In **container mode** (`stcode --headless` in container, `stcode --daemonless` on host),
  the workspace belongs strictly to the **daemon's machine**. The client leaves `cwd` empty
  unless explicitly passed via `--cwd`, allowing the daemon to initialize the agent at its
  own container workspace (`Path.cwd()`, e.g. `/workspace`). If an incoming `cwd` does not
  exist on the daemon filesystem, the daemon logs a warning and falls back to its own
  working directory.

## The protocol

JSONL, one message per line, the same framing on unix and TCP.

```
Client → Daemon
{"type":"create","cwd":"/w","role":"backend-dev"}   → {"type":"session","id":"01M2…"}
{"type":"attach","session":"01M2…"}                 # several clients may attach at once
{"type":"detach"}                                   # stop watching, don't stop the agent
{"type":"sessions"}                                 → what this daemon is holding
{"type":"push","text":"…"}
{"type":"interrupt"}
{"type":"set_mode","mode":"auto-edit"}
{"type":"set_meta","model":"claude-opus-5","reasoning_effort":"high"}
{"type":"info"}                                     → skills, MCP servers, paths
{"type":"get_config"}                               → this daemon's config, keys redacted
{"type":"set_config","defaults":{"model":"claude-opus-5"}}  → writes its config.toml
{"type":"approval","execution_id":"ab12","approved":true}
{"type":"answer","execution_id":"cd34","text":"Postgres"}

Daemon → Client
{"type":"text_delta","text":"…"}
{"type":"tool_started","id":"c1","name":"bash","arguments":{…}}
{"type":"tool_finished","id":"c1","ok":true,"preview":"…"}
{"type":"approval_request","execution_id":"ab12","tool":"bash","arguments":{…}}
{"type":"question","execution_id":"cd34","question":"…","options":[…]}
{"type":"history","records":[…]}                    # replay on re-attach, raw records
{"type":"info","skills":[…],"mcp":[…],"config_path":"…"}
{"type":"config","path":"…","config":{…},"writable":true}  # answer to get/set_config
{"type":"progress","text":"…"}                      # advisory; nothing is recorded
{"type":"text_delta","text":"…","agent":"api-scout"} # a sub-agent's, same frame
{"type":"turn_finished","usage":{…}}                # the full four-field Usage
{"type":"agent_failed","message":"…"}               # a turn ending badly
{"type":"error","message":"…"}                      # a protocol or daemon failure
```

### A line is as long as it needs to be

`asyncio`'s streams cap a line at **64 KiB** by default, and both ends open theirs with
`limit=STREAM_LIMIT` (64 MiB) instead. This is not tuning. `history` is one message
carrying a whole transcript, so at the default a session simply *stopped being
resumable* somewhere around its thirtieth tool call: the client's read loop raised
`ValueError` on the frame, the connection died, and the UI showed an empty screen for a
session whose file on disk was intact. A `push` with a file pasted into it is the same
failure in the other direction.

The frame is built in memory on the sending side either way; the limit only says the
reader is willing to receive what the writer was willing to send. A line that somehow
exceeds even that is reported as an `error` frame rather than ending the read loop in
silence — the silence is what made the original bug take a session file and a hex dump
to diagnose.

Every frame from the daemon carries a `session` id, because several clients may attach to
several sessions over one connection. Client messages take an optional `session` and
default to the last one that connection created or attached to.

Frames from a **sub-agent** carry an `agent` name as well, and nothing else changes: a
`text_delta` from `api-scout` is the same frame with one more field. No wrapper type, for
the same reason agent events are not re-wrapped onto the wire — a parallel set of
sub-agent messages would be a translation layer whose only job is staying in sync.
Absent, `agent` means the session's own agent.

`set_meta` and `info` are the two verbs the TUI needed and the protocol did not have.
`set_meta` appends a `meta` record to the live session, which is how `/model` and
`/effort` reach a running agent without rewriting anything
(→ [sessions](session.md#meta-is-a-merged-view)). `info` answers the questions a client
cannot answer for itself — which skills were found, which MCP servers connected and what
they offer, where the config file is — all of which are facts about the machine the
**daemon** is on, not the one the terminal is on. That distinction is invisible in solo
mode and the whole point in `--daemonless`.

## Five things to get right the first time

**1. Approval is request–response, not a one-way event.** `on_approval` is
`async (ApprovalRequest) -> bool`. Over a socket that becomes: broadcast
`approval_request`, park a `Future` under the `execution_id`, resolve it when a client
answers. `ApprovalRequest` already carried the id and is already a pydantic model, so the
harness needed no change. Same mechanism for `ask_user_question`. Do not "simplify" this
into fire-and-forget.

**2. Detach must not kill the agent.** That is the entire reason the daemon exists. The
*runner*, not the connection, is the single consumer of `agent.events()`; clients are
sinks it fans out to. Losing the last one changes nothing about the turn in flight, and
events keep landing in the session for a re-attaching client to replay.

**3. Approvals live in the runner, not the connection.** The client that asked may be
gone by the time the answer comes, and a second client on the same session may answer
instead.

**4. A `push` arriving mid-turn is delivered at the next tool-call boundary** — never
spliced into the model call in flight. See [agent-loop.md](agent-loop.md#steering-a-push-mid-turn).
To stop the work instead, send `interrupt`.

**5. A parked request whose last client leaves is failed, not left waiting.** Losing the
last watcher fails every pending request with `ToolDenied` **naming the real reason** —
not "the user declined", because a headless agent told a human refused it will act on
that lie.

## An unstarted session is not a session

A session file is not created until its first message
([sessions](session.md#the-file-appears-on-the-first-message)), so `create` can produce a
session id with nothing behind it. When the last client detaches from one of those, the
daemon closes it and drops it from the registry.

Without that, `/clear` — which is a `create` — would leave the daemon holding every
abandoned session for as long as it runs. With it, a client that connects, looks around
and leaves holds nothing open. A session that has written a record is never dropped:
detach does not kill the agent, and that rule has no exceptions.

## Reconfiguring one that is already running

`Daemon.reconfigure(config)` swaps the config and hands the new providers, routing and
retry policy to the gateway **in place**, leaving every session alone.

In place matters because of who holds what: a `/model` that pastes a new key or points a
base URL at a different endpoint has to reach the session that is running *now*, and
that session's `Agent` holds the gateway object rather than this daemon. Building a
fresh gateway here would leave the running turn streaming against the old credentials
until it ended. → [providers.md](providers.md#changing-the-configuration-under-a-running-agent)

What it deliberately does not do is restart anything. `[daemon]` and `[session]`
describe a socket that is already bound and a directory transcripts are already being
written to; changing either is a restart, not a reload.

It is also called for a client that **started this daemon** — the TUI in its default
shape, handing over a key typed into the setup screen — and by `set_config`, which is the
same reload reached from a terminal somewhere else.

## `get_config` / `set_config` — the remote half of `/model`

A `--daemonless` terminal shows settings that belong to the daemon's machine, so it has
to ask for them rather than read its own file. Two messages:

| | |
| --- | --- |
| `get_config` | the whole `GatewayConfig` as the daemon holds it, plus the path it was loaded from. Every `api_key` is replaced with `"***"` before it goes on the wire |
| `set_config` | a patch, and **only `[defaults]`**: `provider`, `model`, `reasoning_effort`, `approval_mode`. Anything else in the frame is ignored |

`set_config` does three things, in order, and reports the result with the same `config`
frame `get_config` answers with:

1. folds the patch into `[defaults]`, and repoints any routing tier that was tracking
   the old default model — the same rule `apply_provider_settings` uses locally, so
   `/model` does not leave the session routing a new model name at the old vendor;
2. `save_config()` to the daemon's own path, which is what makes the change survive a
   container restart;
3. `reconfigure()`, so the gateway the running agent is already holding gets the new
   routing without a reconnect.

**Why `[defaults]` and nothing else.** The socket is the access control, and it is one
boundary, not a graduated one: a client that can drive an agent with your privileges can
already do most things. But a key, a `base_url` or a concurrency cap is not a property
of the conversation — it is how the operator provisioned the container, and it is
usually an environment variable rather than a value in the file at all. Letting a
terminal rewrite those means a `/model` on the wrong tab silently repoints a fleet at a
different endpoint. So they travel one way only: the daemon shows them, redacted, and
does not accept them back.

`guard_autonomy` runs on an `approval_mode` arriving this way, exactly as it does on
`set_mode`. A config file is not a door around rule 5.

## One client, in a container

`[daemon] max_clients` caps concurrent connections. Left unset it is **1 inside a
container and unlimited on the host** — `in_container()` decides, the same function rule
5 already trusts.

The asymmetry is the point. Two terminals watching one session on your laptop is a
feature and the fan-out exists for it. A container is the other case: one container is
one agent is one checkout is one merge boundary (rule 2), and a second terminal steering
the same role is that boundary being crossed by accident rather than by design.

The newcomer is refused, never the incumbent. A connection that is already attached may
be mid-approval, and dropping it to make room turns a stray `stcode --daemonless` into a
way to strand somebody else's turn. The refusal is an `error` frame and then a close, so
the client has something to print rather than a socket that shut without a word.

## `full-auto` and the container rule

`guard_autonomy` runs *before* the bind, and again per session:

```
full-auto + no approver + not in a container  →  AutonomyRefused
```

There is no override flag, and there will not be one. `in_container()` looks for the
signals a real container leaves (`/.dockerenv`, cgroup markers, `STCODE_SANDBOX=1`), and
an attached human does not count as an approver — a human watching a stream is not a
human answering a prompt, and `full-auto` never asks.

This is code, not a warning in a document. The agent runs commands with your privileges;
the container is the mitigation.

## Transports

```toml
[daemon]
transport = "unix"            # "tcp" inside a container
socket    = "~/.stcode/daemon.sock"
host      = "127.0.0.1"
port      = 7717
```

`unix` for solo — no port to collide with, and filesystem permissions are the access
control (the socket is chmod 0600; anyone who can open it can drive an agent with your
privileges). `tcp` for a container, which has no host filesystem to put a socket on. Same
JSONL framing either way, so a WebSocket adapter later is an adapter, not a protocol.

**Transport is configuration, not architecture.** If a feature needs to know which one is
in use, it is in the wrong layer.

## Shutdown

Order is load-bearing and not the obvious one: `Server.wait_closed()` waits for every
*handler*, not just the listening socket, so closing the server while a client is
attached waits forever on a connection that is waiting for you. Clients, then the server,
then the sessions, then the gateway, then the trace exporter's final flush.
