# Setting up a team

Team mode is **N containers, each holding one daemon and one agent with one role**,
sharing one mounted volume.

!!! warning "Multi-agent costs more, not less"

    Anthropic measured multi-agent at ~15× the tokens of chat, and single-agent at ~4×,
    with token volume explaining 80% of performance variance. They list **"most coding
    tasks"** as a *poor* fit.

    Team mode pays off only when roles work on genuinely separable things. Read
    [How a team works](workflow.md) before deciding it is what you want.

## The shape

```
/team/                      <- one volume, mounted into every container
  repo.git/                 <- a bare repository: the origin
  inbox/<role>/             <- one directory per role; a message is a file
  knowledge/                <- shared notes, read and written with the ordinary tools
  artifacts/                <- build outputs, reports
```

Each container clones `/team/repo.git` into its own `/workspace`, works there, and
pushes a branch. Exactly one role merges.

## One image, one config file per agent

```bash
docker build -t stcode .
```

An agent is **one `config.toml`**, prompt included ([why](roles.md)). Deploying a team
is mounting the right one into each container — adding a fifth agent is a file, not a
release:

```bash
docker run -d --name ba \
  -e STCODE_SANDBOX=1 -e STCODE_TEAM=1 -e ANTHROPIC_API_KEY \
  -v team-volume:/team \
  -v "$PWD/examples/agents/ba.toml:/config/config.toml:ro" \
  stcode --headless --transport tcp --host 0.0.0.0 --port 7717 --mode full-auto
```

Repeat with `backend-dev.toml`, `frontend-dev.toml`, `devops.toml`. Each file already
carries its own `[team] role`, its prompt, and whatever model that agent should run at —
a support role on a cheap tier is a line in its own profile rather than a flag you have
to remember at `docker run`.

Mounting a directory of profiles and choosing by name works too, for a host running
several agents off one config:

```bash
  -v "$PWD/examples/agents:/agents:ro" -e STCODE_AGENTS_DIR=/agents
  … --role ba
```

Two environment variables carry the rest:

| | |
| --- | --- |
| `STCODE_SANDBOX=1` | what makes `--mode full-auto` legal. Without it the daemon refuses to start, with no override flag. → [Approval modes](../guide/approval-modes.md) |
| `STCODE_TEAM=1` | turns team mode **on**. It is off by default, so a profile that names a role does not go looking for a `/team` volume on a laptop that has none |

## The origin is a bare repo on the volume

```toml
[team]
enabled    = true
role       = "backend-dev"
shared_dir = "/team"
remote     = "/team/repo.git"
```

A bare repository on the shared volume needs **no credential and no network**, so a
two-container run works locally — and it is still real git: real branches, real merges,
real conflicts.

For pull requests against a real forge instead, point `remote` at a URL and mount a key:

```toml
remote  = "git@github.com:you/project.git"
ssh_key = "/run/secrets/deploy_key"   # mounted read-only, never copied
```

That is one line here and one sentence in the role prompts.

## Driving it

Attach to whichever container you want to talk to:

```bash
uv run stcode --daemonless --transport tcp --host <container-ip> --port 7717
```

Attaching also gives you the daemon's own settings: `/model` in this shape reads and
writes the **container's** `config.toml`, not your laptop's.
→ [`--daemonless`](../guide/shapes.md#it-uses-the-daemons-config-not-yours)

A containerised daemon takes **one client at a time**. Two terminals steering one role is
the merge boundary being crossed by accident; the second connection is refused and told
so. → [One client, in a container](../guide/shapes.md#one-client-at-a-time-in-a-container)

There is **no lead or orchestrator agent**. You dispatch, per role. The BA is the usual
entry point purely by convention, written into `ba.toml` — a `lead` would be one more
profile, not a new mechanism.

## Waking on a message

```toml
[team]
enabled         = true
wake_on_message = true
poll_interval   = 1.0
```

When a message lands and the agent is **idle**, the daemon starts a turn. A message
arriving mid-turn is not interrupted into — the agent drains its inbox at the top of
the next turn anyway.

Polling, not inotify: one directory listing a second costs nothing measurable, behaves
the same on every filesystem a volume might be, and needs no dependency.
