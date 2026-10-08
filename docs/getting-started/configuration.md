# Configuration

The essentials for getting a working setup. Every section and every key is documented
in the [configuration reference](../architecture/configuration.md); this page is what
you actually need on day one.

## Where the file is

| | |
| --- | --- |
| Linux / macOS | `~/.stcode/config.toml` |
| Windows | `%APPDATA%\stcode\config.toml` |
| Override | `STCODE_HOME=/dir` for the whole directory, `STCODE_CONFIG=/path/to/config.toml` for the file |

```bash
uv run stcode config     # asks the daemon: the path and what it selects
```

The file belongs to the daemon, which creates a minimal one on first start. The UI
never edits it directly: `/model`, `/mode` and `/effort` ask the daemon to change single
keys, so **your comments and formatting survive**.

The theme and the folders you have trusted are **not** in here — they live in
`~/.stcode/ui.toml`, because they are facts about your terminal rather than about the
agent. `?` in the TUI shows both paths.

## The keys that matter

```toml
[model.providers.anthropic]           # the name is yours
provider = "anthropic"                # the library: openai | anthropic | google
api_key  = "${ANTHROPIC_API_KEY}"     # a reference to a variable, or the key itself
model    = "claude-opus-5"            # unset = the library's default

[agent]
approval_mode    = "suggest"
reasoning_effort = "medium"
```

That is a complete, working config. With one provider entry, everything uses it.

!!! tip "Prefer `"${VAR}"` to a literal key"

    A `"${ANTHROPIC_API_KEY}"` reference is read from the daemon's environment when it
    is used, so the key never touches disk. A literal is honoured — the setup screen
    writes one when you paste a key — and is why the file is `chmod 0600`. Leave
    `api_key` out entirely and the library's conventional variable is used.

## Two accounts, and cheaper models for cheap work

```toml
[model]
default = "work"                      # which entry answers when no tier says otherwise

[model.providers.work]
provider = "openai"
api_key  = "${WORK_OPENAI_KEY}"

[model.providers.personal]
provider = "openai"
api_key  = "${OPENAI_API_KEY}"

[model.routing.low]                   # optional, per difficulty tier
provider = "personal"
model    = "gpt-5-mini"
```

Every call declares a difficulty, and a `[model.routing]` line for it decides the
model; with no line, the default entry answers.

| Tier | Who asks for it |
| --- | --- |
| `high` | the agent's own turns, by default (`[agent] difficulty`) |
| `low` | the [supervisor](../architecture/supervisor.md), when a heuristic fires |

## Command-line flags

Flags apply to **this run only** and are never written to the file — `--mode full-auto`
once must not leave `full-auto` there forever.

```bash
uv run stcode ~/projects/api --mode auto-edit
uv run stcode --transport tcp --host 10.0.0.4 --port 7717 --daemonless
```

| Flag | What it does |
| --- | --- |
| `--cwd` / a leading path | the directory the agent works in |
| `--mode` | the approval mode of the sessions this run opens |
| `--role` | which agent this is (`[team] role`). The prompt comes from `[agent] prompt` |
| `--config` | the file a daemon started by this run reads |
| `--restart` | stop the local daemon first and start a fresh one |
| `--transport` `--socket` `--host` `--port` | where the daemon is (and where to start one) |
| `--team` | `--headless` only: turn team mode on. Otherwise `STCODE_TEAM=1` or `[team] enabled` |

## Narrowing the tool set

```toml
[agent]
tools = ["read", "grep", "glob", "ls"]   # an allow-list, replacing the default set
exclude_tools = ["web_search"]           # subtracted from whatever is left
```

A name that matches no tool **refuses to start**. A config that quietly produced a
smaller tool set would be diagnosed as "the model is ignoring its tools", weeks later.
