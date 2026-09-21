# 0005 — An agent profile is a config file

**Status: accepted.** Supersedes the "one markdown file per role" arrangement in
[0003](0003-what-the-tui-owns.md)'s era.

## Context

A role was `<role>.md`: a prompt, and nothing else. Everything else about that agent —
which model it runs, which tier, which tools it may not have, whether it is in plan mode
— lived in `config.toml`. Deploying a team meant mounting **two** things per container
and keeping them in step:

```bash
-v ./agents:/agents:ro -e STCODE_AGENTS_DIR=/agents -e STCODE_ROLE=backend-dev
-v ./config.toml:/config/config.toml:ro
```

Two mounts that have to agree, and one shared `config.toml` across four containers that
should not be running the same model. Putting a BA on a cheap tier meant a config file
per role anyway — at which point the markdown file is a second file describing the same
agent, for no reason but history.

A second problem sat next to it: `[team] role` was doing two jobs. It named the agent
*and* switched team mode on. A profile copied to a laptop to borrow its prompt started
polling `/team` and refused to start.

## The options

### A. Keep `.md`, add `[agent] model` and friends to it

Front-matter in the markdown. Now there are two config formats, one of them invented
here, and `GatewayConfig` validates only one of them.

### B. Keep both files, add `[agent] prompt_file` to the config

Two mounts still, but they point at each other. Better than nothing; does not remove the
pair.

### C. The profile *is* a `config.toml`, with the prompt in `[agent] prompt`

One file. It is an ordinary config, so there is nothing new to parse, nothing new to
validate, and mounting it as `/config/config.toml` makes it the container's whole
configuration.

## The decision

**C**, with `[agent] prompt_file` from B kept as the escape hatch for a prompt long
enough to want its own file — resolved relative to the profile, so a directory of them
still moves as a unit. Setting both keys is an error, not a precedence rule: a
precedence rule is one more thing to be wrong about on the day a container comes up
wearing the wrong prompt.

```bash
docker run -v ./agents/backend-dev.toml:/config/config.toml:ro … stcode
```

Deploying N agents is N mounts of one path.

**Looked up by name, only the prompt is read.** `--role backend-dev` on a machine that
holds several profiles takes that profile's `[agent] prompt` and nothing else; the
model, the socket and the session directory come from the config that was actually
loaded. Two files both claiming to configure the daemon is a support question about
which one won.

**And `[team] enabled` is now its own key**, off by default, with `STCODE_TEAM=1` and
`--team` as the other two spellings. A role says *which agent this is*. Team mode says
*a shared volume is mounted*. They were one key because in the first deployment they
were always true together.

## Consequences

- **`load_role` is gone**, and so are `examples/agents/*.md` — converted, not kept
  alongside. Two supported formats is the thing this record exists to remove.
- **A prompt now lives in a TOML string.** `tomli_w` round-trips it, and
  `tests/core/configs_test.py` asserts that with quotes and newlines in it, because a
  prompt is the one value here that has both.
- **`GatewayConfig` remembers where it was read from** (`source_path`, excluded from the
  dump) so `prompt_file` can be relative to it. A config object that does not know its
  own origin cannot resolve a relative path, and resolving it at load time would mean
  `prompt` and `prompt_file` both set on the next save.
- **`--role` no longer joins a team**, which is a behaviour change for anybody relying
  on it. It is loud in the other direction now: `[team] enabled` with no role refuses to
  start, because a mailbox with no owner addresses every message to `""`.
- **An agent's model is now a property of the agent.** A BA on `medium` is a line in
  `ba.toml` rather than a flag somebody has to remember at `docker run` — which is most
  of the point.

## What would change this

If profiles ever need to *compose* — a base config plus per-agent overlays, so twelve
agents do not repeat the same `[providers]` block twelve times — that is an include
mechanism, and it is a different decision from this one. The shape to reach for first is
the one already there: `[providers]` from environment variables, which is what the
Dockerfile does, leaving a profile to carry only what differs.

If a prompt ever needs to vary at run time — templated per task, assembled from parts —
it stops being a deployment fact and this record does not cover it. Note that hard rule 6 — the agent never edits its own harness — still
forbids the agent editing its own.
