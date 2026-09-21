# Configuration

`core/configs.py` owns *how the file is structured* and what reads it — TOML, `.env`,
every `[section]` model — and nothing else in the codebase parses TOML.

*Where* it lives is `core/common/paths.py`. That split is not tidiness: `core/configs.py`
imports `core/harness/`, and `core/harness/prompts/` needs the config **directory** to
find role profiles, so the location cannot live in `configs.py` without a cycle. Both
names are re-exported from `core/configs.py`, so `from stcode.core.configs import
default_config_path` still works and always has.

```
~/.stcode/config.toml                  # or %APPDATA%\stcode\config.toml on Windows
$STCODE_CONFIG                         # overrides both — what a container sets
```

Written 0600, atomically (write-then-rename), because it may hold a literal API key. The
UI rewrites the whole file from the model, so hand-added comments do not survive a save —
values do.

## What is *not* in this file

The TUI's own state — the theme, the folders you have trusted, and how long a `!`
command may run — is in `~/.stcode/ui.toml`, read by the terminal client and by nothing
else. `config.toml` is what a `--headless` daemon reads, and a container has no theme,
trusts nothing, and never runs a `!`.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

```toml
# ~/.stcode/ui.toml
theme         = "auto"        # auto | dark | light
trusted       = ["/home/you/work/stcode"]
shell_timeout = 10            # seconds a `!` command may run. Clamped to 1–120
```

## Credentials

Two ways, and **the environment wins when set**:

```toml
[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"      # preferred: nothing secret touches disk
api_key     = "sk-…"                   # what the setup screen writes, if you paste one
base_url    = "https://…"              # any endpoint speaking the provider's protocol

max_concurrent = 0                     # completions this endpoint will serve at once
```

`max_concurrent` is the one key here that is not a credential. It caps how many
completions are in flight against **this endpoint**, across every session, every
sub-agent and the supervisor, because one daemon shares one gateway. `0` is no cap,
which is right for a hosted API. Set it to `1` for Ollama or llama.cpp: five parallel
`task` calls are otherwise five simultaneous requests to a server holding one model on
one GPU, and it drops the ones that waited too long. → [providers.md](providers.md)

`.env` files are loaded most-specific first — `./.env`, then `~/.stcode/.env` — and real
process environment variables beat both.

## Every section

### `[defaults]` — what the TUI starts with

| Key | Default | |
| --- | --- | --- |
| `provider` | `"anthropic"` | what `/model` last selected |
| `model` | `""` | empty means "not chosen yet", which the chat screen warns about rather than guessing |
| `reasoning_effort` | `""` | `none` … `max`; empty leaves it to the model. What `/effort` writes |
| `approval_mode` | `"suggest"` | `plan` / `suggest` / `auto-edit` / `full-auto` |

Session defaults, not routing policy: the gateway ignores them.

### `[providers.<name>]` and `[routing.<tier>]`

See [providers.md](providers.md). Three tiers — `low`, `medium`, `high` — each naming a
provider and model, with optional per-tier credential overrides.

### `[retry]`

`max_attempts = 3`, `base_delay = 1.0`, `max_delay = 20.0`, `jitter = true`. Applies only
before the first token.

### `[agent]` — limits on one turn

| Key | Default | |
| --- | --- | --- |
| `max_turns` | `40` | tool-call ceiling **within** one turn, not a conversation length |
| `max_depth` | `1` | not meant to be raised: recursion with no budget is a fork bomb |
| `max_concurrent` | `4` | tools running at once |
| `enable_task` | `true` | whether sub-agents exist |
| `difficulty` | `"high"` | the tier the main loop uses |
| `tools` | `[]` | an allow-list **replacing** the default set. Empty means the default |
| `exclude_tools` | `[]` | subtracted from whatever is left |
| `prompt` | `""` | this agent's own prompt, inline. Sits high in the cached prefix |
| `prompt_file` | `""` | the same thing in a file, resolved **relative to this config file** |

Both tool lists are applied after the agent is fully assembled, so `task`, `send_message`
and MCP tools can be named. A name matching no tool refuses to start, with the list of
real ones.

`prompt` and `prompt_file` are what make one config file a whole agent — see
[agent profiles](harness.md#an-agent-profile-is-one-configtoml). Setting both is an
error rather than a precedence rule to memorise. Leaving both empty and naming a
`[team] role` looks the profile up by name in the agents directory instead.

### `[session]`

`dir` (a path; `./.stcode/sessions` keeps transcripts with the project) and `keep`, the
number of session files `prune` leaves behind.

### `[daemon]`

| Key | Default | |
| --- | --- | --- |
| `transport` | `"unix"` | `unix` on your machine, `tcp` in a container |
| `socket` | `~/.stcode/daemon.sock` | `unix` only |
| `host` / `port` | `127.0.0.1` / `7717` | `tcp` only |
| `max_clients` | *unset* | concurrent connections. **Unset = 1 in a container, unlimited on the host**; `0` is unlimited everywhere |

See [daemon.md](daemon.md#one-client-in-a-container).

### `[mcp]`

`enabled`, and `expose` (`code` / `tools`). See [mcp.md](mcp.md).

### `[supervisor]`

`enabled`, `every`, `window`, `difficulty`. See [supervisor.md](supervisor.md).

### `[team]`

| Key | Default | |
| --- | --- | --- |
| `enabled` | `false` | **the switch.** Team mode is off until this is true |
| `role` | `""` | which agent this is. With `enabled`, it must name a mailbox owner |
| `shared_dir` | `/team` | the mounted volume |
| `max_agents` | `6` | |
| `wake_on_message` | `true` | start a turn when mail arrives and the agent is idle |
| `poll_interval` | `1.0` | seconds between inbox checks |
| `remote` | `/team/repo.git` | the bare origin. Point at a URL for real pull requests |
| `ssh_key` | `""` | only needed if `remote` is a URL. Mounted read-only, never copied |

`enabled` and `role` are two facts, and merging them was a mistake worth undoing. A role
says *which agent this is* and is useful on its own — it selects the profile, and
therefore the prompt, in a perfectly ordinary solo session. Team mode says *a shared
volume is mounted*, and getting that wrong means an agent that refuses to start looking
for an inbox that is not there. So naming a role no longer implies a team:

```toml
[team]
enabled = true          # or STCODE_TEAM=1
role    = "backend-dev"
```

Off by default, in both places. The failure this prevents is the quiet one: a config
copied from a teammate, or a `--role` typed to pick up a prompt, used to turn on mailbox
polling and a `/team` write scope on a laptop that has neither.

### `[trace]`

`enabled`, `endpoint`, `headers`, `service_name`, `content`. Off by default; needs the
`otel` extra. See [tracing.md](tracing.md).

## Command-line overrides

```bash
stcode --mode auto-edit --model claude-sonnet-5 --role backend-dev --port 7717
```

`apply_cli_overrides` returns a **copy** with these folded in, and leaves the loaded
config alone. That is what makes "for this run only" true rather than aspirational: the
UI rewrites its config whenever a mode or an address changes, so a flag folded into that
same object would go to disk with it — and `--transport tcp`, used once to reach a
container, would become the transport every later bare `stcode` binds.

`--role` picks the agent profile. It does **not** turn team mode on — `--team`, or
`STCODE_TEAM=1`, does that.

## Environment variables

| | |
| --- | --- |
| `STCODE_CONFIG` | where the config file is |
| `STCODE_AGENTS_DIR` | one directory of agent profiles (`<name>.toml`), and nothing else — what a container mounts |
| `STCODE_ROLE` | the role, same as `[team] role`. Selects a profile; does not turn team mode on |
| `STCODE_TEAM=1` | turn team mode on, same as `[team] enabled` |
| `STCODE_SANDBOX=1` | this is a contained environment; unlocks `full-auto` |
| `STCODE_MCP_CONFIG` | an `.mcp.json` somewhere other than the workspace |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY` | the conventional names |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | used when `[trace]` leaves them empty |
