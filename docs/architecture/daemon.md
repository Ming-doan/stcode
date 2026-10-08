# The daemon

`core/daemon/`. **One daemon, many sessions**, addressed by id. A TUI is a client of it.

That inversion — daemon first, UI second — is what makes the container story real rather
than aspirational: moving the agent into a container is a config key, not a rewrite.

The daemon is its own program, `stcode-daemon` (`python -m stcode.core`). `stcode/cli/`
and `stcode/core/` never import each other: the UI knows the daemon only as a command it
can start and a socket it can speak JSONL to, which is what lets `core/` be rewritten in
another language without touching the UI. → [decision 0006](../decisions/0006-cli-and-core-are-separate-programs.md)

```py
async with Daemon(config) as daemon:          # binds; `full-auto` refuses here
    await daemon.serve_forever()              # until stop(), SIGTERM, or `shutdown`

# stcode/cli/services/client.py — the other end, sharing only the wire format
async with await DaemonClient.connect(Address()) as client:
    await client.create(cwd=Path.cwd())
    await client.push("Add rate limiting")
    async for frame in client.events(): ...
```

Daemon modules, one job each:

| | |
| --- | --- |
| `protocol.py` | the JSONL message shapes — the only thing on the wire |
| `runner.py` | `SessionRunner`: one live session, its watchers, its parked requests |
| `server.py` | the socket, the registry, one connection object per client, config reloads |
| `workspace.py` | explicit user shell commands and bounded file discovery in the session workspace |
| `autonomy.py` | `guard_autonomy` / `in_container` — the `full-auto` rule, as code |
| `main.py` | `stcode-daemon`: flags, the startup report, signals, exit codes |

## The three shapes

```bash
stcode                # UI + a daemon, started here if none is listening
stcode --headless     # the daemon alone — what a container runs
stcode --daemonless   # the UI alone, attached to a daemon elsewhere
```

No implementation switches between them. `--headless` replaces the `stcode` process
with `stcode-daemon`. Plain `stcode` connects to `~/.stcode/daemon.sock` (or the address
its flags name); if nothing answers, `cli/services/launcher.py` starts `stcode-daemon` on that
address as a subprocess, logging to `~/.stcode/daemon.log`, and stops it again when the
UI exits. A daemon it *found* is left running — quitting is a detach. `stcode --restart`
sends `shutdown` to whatever is listening first, for a daemon that is wedged or running
old code.

`--daemonless` never starts an agent locally: if nothing answers it asks *which
daemon?* rather than quietly running the work on your laptop. `/connect` moves a running
UI to a different one.

`stcode-daemon` exits `0` when stopped, `2` when rule 5 refuses `full-auto`, and `1` for
anything else that stopped it starting. The launcher shows the tail of the log in the
last two cases, so the user reads the reason rather than "connection refused".

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

`!` shell commands and `@` file completion also use the attached session's directory
on this machine. Their `workspace_shell` and `workspace_files` requests carry a
`request_id`; replies echo it, so concurrent commands cannot steal each other's
results. Connection-owned tasks keep slow commands off the reader and are cancelled
on disconnect. Shell processes are killed as a group on timeout or disconnect.
These user actions never enter the agent's session history.

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
{"type":"get_config"}                               → this daemon's config, no keys
{"type":"set_config","provider":"work","model":"gpt-5","api_key":"…"}  → writes its config.toml
{"type":"shutdown"}                                 # unix socket only
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
{"type":"config","path":"…","provider":"…","model":"…","providers":{…},…}  # get/set_config
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

## Configuration over the socket

The daemon is the only thing that reads or writes `config.toml`. Every client — the UI
that started it as much as a terminal attached from elsewhere — asks for it:

| | |
| --- | --- |
| `get_config` | a `config` frame: the file's path on **the daemon's** disk, whether it is writable, whether keys may be sent, the default provider entry and model, effort, mode, every provider entry with `has_key` instead of its key, and the libraries a new entry can use |
| `set_config` | a patch of `provider`, `model`, `api_key`, `reasoning_effort`, `approval_mode`. Answered with a `config` frame whether or not it was accepted |

`provider` names a `[model.providers]` entry, or a library name, which creates that
entry; it becomes `[model] default`. `model` and `api_key` apply to that entry.

`Daemon.set_config` does three things, in order:

1. `update_config()` patches those keys in the file — comments survive, and a patch that
   would not load is refused before anything is written;
2. `reload()` re-reads the file with this process's command-line overrides on top,
   keeping `[daemon]` as bound — the socket is already open;
3. hands the new `[model]` to the gateway **in place** and the new default effort to
   every live agent.

In place matters because of who holds what: a key typed into `/model` has to reach the
session that is running *now*, and that session's `Agent` holds the gateway object
rather than this daemon. → [providers.md](providers.md#changing-the-configuration-under-a-running-agent)

**Keys only over the unix socket.** A unix socket is chmod 0600: whoever connects is
this user on this machine. A TCP client could be anyone who can reach the port, and a
key typed into the wrong tab would repoint a fleet. So `api_key` — and `shutdown` — are
refused over TCP, and `keys_editable` in the frame tells the UI to disable the field
rather than offer an edit that will fail. Keys never travel the other way at all.

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

A refused interactive mode switch reports a warning and keeps the existing mode and
connection. Startup still refuses an unsafe full-auto configuration.

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

**Transport is configuration, not architecture.** The one thing that reads it is
`Daemon.local_only`, which decides what only this machine's user may do — send a key,
stop the daemon. Anything else that needs to know is in the wrong layer.

## Shutdown

`serve_forever()` returns when `stop()` is called — by a SIGTERM or SIGINT handler, or by
a `shutdown` message over the unix socket. `aclose()` then tears down.

Order is load-bearing and not the obvious one: `Server.wait_closed()` waits for every
*handler*, not just the listening socket, so closing the server while a client is
attached waits forever on a connection that is waiting for you. Clients, then the server,
then the sessions, then the gateway, then the trace exporter's final flush.
