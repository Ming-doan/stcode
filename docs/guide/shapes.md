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

It prints the address it is listening on and then serves until killed. This is also the
only shape in which `full-auto` can run unattended — and only inside a container. →
[Approval modes](approval-modes.md)

## `--daemonless` — the UI alone

Attaches to a daemon somewhere else and **never starts one locally**. If nothing
answers, it asks *which daemon?* rather than quietly running the work on your laptop —
which would be exactly the wrong thing to do silently when you meant to drive a
container.

`/connect` moves a running UI to a different daemon without restarting it.

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
