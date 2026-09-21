# Configuration

The essentials for getting a working setup. Every section and every key is documented
in the [configuration reference](../architecture/configuration.md); this page is what
you actually need on day one.

## Where the file is

| | |
| --- | --- |
| Linux / macOS | `~/.stcode/config.toml` |
| Windows | `%APPDATA%\stcode\config.toml` |
| Override | `STCODE_CONFIG=/path/to/config.toml` |

```bash
uv run stcode config     # prints the path and what it selects
```

It is created on first run with a commented default. Saving from the UI rewrites it
from the in-memory model, so **hand-written comments are not preserved** — keep
anything you want to remember in version control, not in the file.

The theme and the folders you have trusted are **not** in here — they live in
`~/.stcode/ui.toml`, because they are facts about your terminal rather than about the
agent. `?` in the TUI shows both paths.

## The four keys that matter

```toml
[defaults]
provider = "anthropic"
model = "claude-opus-5"
approval_mode = "suggest"

[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"   # a variable NAME, not the key

[routing.high]
provider = "anthropic"
model = "claude-opus-5"
```

`[defaults]` is what the UI shows and edits. `[routing]` is what the gateway actually
calls — three tiers, `low` / `medium` / `high`, so a cheap model can do cheap work.

!!! tip "Prefer `api_key_env` to `api_key`"

    `api_key_env` names an environment variable read at request time; `api_key` is the
    literal. The environment wins when set. A literal is honoured — the setup screen
    writes one there — and is why the file is `chmod 0600`.

## Difficulty tiers

Every call declares a difficulty, and the tier decides the model:

| Tier | Who asks for it |
| --- | --- |
| `high` | the agent's own turns, by default (`[agent] difficulty`) |
| `low` | the [supervisor](../architecture/supervisor.md), when a heuristic fires |
| `medium` | the fallback when a tier has no route configured |

One missing tier is not a reason to refuse to work: an unrouted difficulty falls back
to `medium`, then `high`, then `low`, and only a config with no routes at all raises.

## Useful overrides

Command-line flags apply to **this run only** and are never written back — `--mode
full-auto` once must not leave `full-auto` in the file forever.

```bash
uv run stcode --cwd ~/projects/api --mode auto-edit --model claude-sonnet-5
uv run stcode --transport tcp --host 10.0.0.4 --port 7717
```

| Flag | What it overrides |
| --- | --- |
| `--cwd` | the directory the agent works in |
| `--mode` | `[defaults] approval_mode` |
| `--model` | `[defaults] model` |
| `--role` | `[team] role` — which agent this is, and so which profile supplies the prompt |
| `--team` / `--no-team` | `[team] enabled`. Team mode is **off** unless this, `STCODE_TEAM=1`, or the config says otherwise |
| `--config` | which file to load |
| `--transport` `--socket` `--host` `--port` | `[daemon]` |

## Narrowing the tool set

```toml
[agent]
tools = ["read", "grep", "glob", "ls"]   # an allow-list, replacing the default set
exclude_tools = ["web_search"]           # subtracted from whatever is left
```

A name that matches no tool **refuses to start**. A config that quietly produced a
smaller tool set would be diagnosed as "the model is ignoring its tools", weeks later.
