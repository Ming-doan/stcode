# Roles

A role is **one `config.toml`** — the config the agent's daemon loads. It is data, not
code, and it is not shipped in the package. There is no lookup by name and no fallback:
the config that was loaded *is* the agent.

Copies to start from live in `examples/agents/`: `ba.toml`, `backend-dev.toml`,
`frontend-dev.toml`, `devops.toml`.

## Why a config file and not a markdown file

Because an agent is a prompt *and* the settings it runs under, and they were in two
places that had to be kept in step by hand. The profile below says everything about this
agent in one file — which is also exactly what a container needs mounted:

```bash
docker run -v ./agents/backend-dev.toml:/config/config.toml … stcode
```

One mount per agent, and the mount *is* the config. Nothing to look up, nothing to
forget.

## What goes in one

```toml
# agents/backend-dev.toml

[model.providers.anthropic]
provider = "anthropic"
api_key  = "${ANTHROPIC_API_KEY}"
model    = "claude-sonnet-5"

[team]
role        = "backend-dev"
description = "Owns src/api/ and src/services/."

[agent]
difficulty = "medium"
prompt = """
# Role: backend developer

## You own
`src/api/` and `src/services/` — the HTTP surface and the business logic behind it.

## You do not own
The database schema (that is `data-eng`), deployment, or anything under `infra/`.
Message the owner instead of editing across the line.

## How you report
When a change is ready, send the branch name and the paths you touched.
Never paste file contents into a message.
"""
```

Three sections in the prompt, and each one exists to prevent a specific failure:

| Section | The failure it prevents |
| --- | --- |
| **You own** | two agents editing the same file |
| **You do not own** | an agent helpfully fixing something outside its boundary |
| **How you report** | messages that carry content instead of paths |

Everything outside `[agent] prompt` is an ordinary config section, so a support role on a
cheap tier, a devops role with `exclude_tools`, or a BA in `plan` mode is a line in its
own profile rather than an argument you have to remember at `docker run`.

A prompt long enough to want its own file gets `prompt_file = "backend-dev.md"`, resolved
**relative to the profile**, so the directory still moves as a unit. Setting both is an
error rather than a precedence rule.

`[team] description` is the one line the other agents see when they call
`find_teammate`. Write it for them: what you own, so they know when to hand off to you.

## One agent = one role = one checkout = one merge boundary

If two roles need to write the same file, **the roles are split wrong**. That is a
design error, not a signal to add locking.

The boundary is drawn at the *service*, not the file, for exactly this reason: a split
that produces overlapping ownership produces merge conflicts that no amount of
coordination machinery fixes.

It is also why a containerised daemon takes
[one client at a time](../guide/shapes.md#one-client-at-a-time-in-a-container).

## The role is in the cached prefix

A role is static for the life of a container, so its text sits high in the system
prompt, above the general guidance and inside the cached prefix. It outranks the
general advice about what to work on, and it costs nothing after the first turn.

## Solo mode and roles

A config with a role and a prompt works solo too. It does **not** turn team mode on — that
takes `[team] enabled`, or `STCODE_TEAM=1`, which is a deployment declaring that a shared
volume is mounted. A solo session naming a role should not go looking for an inbox.

```bash
STCODE_CONFIG=agents/backend-dev.toml uv run stcode          # that agent, solo
STCODE_CONFIG=agents/backend-dev.toml uv run stcode --team   # and joined to /team
```

Team mode is off by default in both the config and the environment. The failure that
prevents is the quiet one: a profile copied from a teammate used to switch on inbox
polling and a `/team` write scope on a machine that has neither.
