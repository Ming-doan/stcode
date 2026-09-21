# Roles

A role is **one `config.toml`**. It is data, not code, and it is not shipped in the
package.

```
~/.stcode/agents/backend-dev.toml
<project>/.stcode/agents/backend-dev.toml
$STCODE_AGENTS_DIR/backend-dev.toml
```

Searched nearest-first, with **no bundled fallback**. That is deliberate: a silent
fallback would mean a container whose volume failed to mount still starts — as somebody
else's backend dev. An unknown role name raises instead.

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

[defaults]
provider = "anthropic"
model    = "claude-sonnet-5"

[team]
role = "backend-dev"

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

!!! note "When the profile is looked up by name"

    Mounted as the container's config, the whole file is the config — every section
    applies.

    Found by name (`--role backend-dev`, or `[team] role` in a shared config), **only
    the prompt is read from it.** The model, the socket and the session directory come
    from the config that was actually loaded. Two files both claiming to configure the
    daemon is a support question about which one won.

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

Naming a role gives the agent that prompt. It does **not** turn team mode on — that
takes `[team] enabled`, or `STCODE_TEAM=1`, which is a deployment declaring that a shared
volume is mounted. A solo session naming a role should not go looking for an inbox.

```bash
uv run stcode --role backend-dev          # that prompt, solo
uv run stcode --role backend-dev --team   # that prompt, and a mailbox on /team
```

Team mode is off by default in both the config and the environment. The failure that
prevents is the quiet one: a profile copied from a teammate used to switch on inbox
polling and a `/team` write scope on a machine that has neither.
