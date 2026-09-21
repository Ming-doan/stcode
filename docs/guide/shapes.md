# The three shapes

One binary, three shapes, and no implementation switches between them. The difference
is only *which halves run where*.

```bash
uv run stcode                # UI + a daemon, started here if none is listening
uv run stcode --headless     # the daemon alone — what a container runs
uv run stcode --daemonless   # the UI alone, attached to a daemon elsewhere
```

## Why it is built this way

The agent lives in the daemon. The TUI is a socket client that sends `push` and renders
whatever comes back. That inversion is what makes three separate-looking deployments
one piece of code:

- **Closing the window does not stop the work.** Detach is a protocol message; the
  agent never hears about it.
- **Two terminals can watch one session.** The daemon fans events out to every attached
  client, and either of them can answer an approval.
- **Moving into a container is a config key.** `transport = "tcp"` instead of `unix`.
  Same JSONL framing on both.

## `stcode` — the normal case

Looks for a daemon at the configured address. If one answers, it attaches. If nothing
does, it starts one in the background and attaches to that. You will not usually notice
which happened.

The session's workspace is the current directory, or one you name:

```bash
uv run stcode                # here
uv run stcode ~/code/api     # there, without cd-ing first
uv run stcode . --mode plan  # the path comes first, flags after
```

`stcode <path>` and `--cwd <path>` are the same thing. The path has to come **first**,
because the argument is read before the subcommand is chosen — otherwise `stcode
sessions` would mean "open the directory ./sessions". A first word that is neither a
flag, a subcommand, nor anything path-shaped is left alone, so a mistyped command still
gets told it is not a command.

## `--headless` — the daemon alone

No UI, no terminal control, just a socket. This is what a container's entrypoint runs.

```bash
uv run stcode --headless --transport tcp --host 0.0.0.0 --port 7717
```

### What it prints

A container's daemon has no screen, so its start-up is the only place it can say what it
became. It prints a report, then logs every connection, then serves until killed:

```
stcode 0.1.0 — headless daemon
  listening   tcp 0.0.0.0:7717   (1 client max)
  workspace   /workspace
  mode        suggest
  model       claude-sonnet-5 (anthropic)
  config      /config/config.toml
  sessions    /root/.stcode/sessions
  agents      /agents  (ba, backend-dev, devops, frontend-dev)
  team        backend-dev on /team
  container   yes (STCODE_SANDBOX)
  created     /root/.stcode/sessions
ready — ctrl-c to stop
[info] client connected: 172.17.0.1:52418 (1/1)
[info] session 01JC… created at /workspace
[info] client disconnected: 172.17.0.1:52418
```

Everything in it answers a question you would otherwise have to exec into the container
to ask. Two lines are worth calling out:

- **`created`** lists the files and directories this start-up *made*. A config that was
  scaffolded because the mount silently did not happen is the single most expensive
  thing to discover late, and it looks identical to a config that was mounted — until
  the daemon says which one it was.
- **`container`** is [`in_container()`](../architecture/daemon.md) reporting itself,
  because it is what decides whether `full-auto` is allowed to start at all.

Logging is at `INFO` in this shape and nowhere else. A UI has a transcript; a daemon has
stdout, and `docker logs` is how you read it.

### One client at a time, in a container

A daemon that detects a container accepts **one connection**. A second one is told so
and closed; the attached client keeps the session.

```
$ stcode --daemonless --transport tcp --host 10.0.0.4 --port 7717
this daemon already has a client attached (1/1)
```

The rule is narrow on purpose. On your own machine two terminals watching one session is
a feature, and it stays. A container is the opposite case: one container is one agent is
one checkout is one merge boundary, and a second terminal steering the same role from
somewhere else is that boundary being crossed by accident.

Set it yourself if the default is wrong for you:

```toml
[daemon]
max_clients = 0   # unlimited; omit the key for "1 in a container, unlimited on the host"
```

This is also the only shape in which `full-auto` can run unattended — and only inside a
container. → [Approval modes](approval-modes.md)

## `--daemonless` — the UI alone

Attaches to a daemon somewhere else and **never starts one locally**. If nothing
answers, it asks *which daemon?* rather than quietly running the work on your laptop —
which would be exactly the wrong thing to do silently when you meant to drive a
container.

`/connect` moves a running UI to a different daemon without restarting it.

### It uses the daemon's config, not yours

Once attached, this shape reads its settings **from the daemon**. The model, provider,
routing tiers, approval mode and paths it shows are the ones in the container's
`config.toml` — not the ones in `~/.stcode/config.toml` on your laptop, which describe a
different machine's agent.

`/model` writes back. It sets `[defaults]` in the *daemon's* config file and reconfigures
the gateway in place, so the change outlives the session and the container restart:

```
/model                       # the card lists what the daemon can reach
→ provider and model written to /config/config.toml on the daemon
```

**Only `[defaults]`.** API keys, `base_url`, routing tiers and concurrency caps are shown
read-only and never sent. Those are the operator's, and a terminal that can rewrite the
credentials of every daemon it can reach is a worse tool than one that cannot. Change
them where the container is deployed.

Your local `~/.stcode/config.toml` is left alone in this shape, apart from the daemon
address `/connect` succeeded on — which is a fact about *your* terminal, so it is stored
here.

## Attaching to an agent in a container

```bash
# in the container
docker run -e STCODE_SANDBOX=1 -e ANTHROPIC_API_KEY -p 7717:7717 \
    -v "$PWD:/workspace" stcode --headless --transport tcp --host 0.0.0.0

# on your machine
uv run stcode --daemonless --transport tcp --host <container-ip> --port 7717
```

!!! danger "The socket is the access control"

    Anyone who can open the socket can drive an agent with the daemon's privileges. On
    unix that is filesystem permissions and the socket is created `0600`. On TCP it is
    whatever your network gives you — do not bind `0.0.0.0` on a machine you share.

## Transports

| | `unix` | `tcp` |
| --- | --- | --- |
| Default for | your own machine | a container |
| Address | `~/.stcode/daemon.sock` | `host:port` |
| Access control | filesystem permissions | the network |
| Framing | JSONL | the same JSONL |

Transport is configuration, not architecture. A third one later is an adapter, not a
protocol change. → [The daemon](../architecture/daemon.md)
