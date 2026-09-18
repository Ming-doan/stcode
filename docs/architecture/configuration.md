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

The TUI's own state — the theme, and the folders you have trusted — is in
`~/.stcode/ui.toml`, read by the terminal client and by nothing else. `config.toml` is
what a `--headless` daemon reads, and a container has no theme and trusts nothing.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

## Credentials

Two ways, and **the environment wins when set**:

```toml
[providers.anthropic]
api_key_env = "ANTHROPIC_API_KEY"      # preferred: nothing secret touches disk
api_key     = "sk-…"                   # what the setup screen writes, if you paste one
base_url    = "https://…"              # any endpoint speaking the provider's protocol
```

`.env` files are loaded most-specific first — `./.env`, then `~/.stcode/.env` — and real
process environment variables beat both.

## Every section

### `[defaults]` — what the TUI starts with

| Key | Default | |
| --- | --- | --- |
| `provider` | `"anthropic"` | what `/model` last selected |
| `model` | `""` | empty means "not chosen yet", which the chat screen warns about rather than guessing |
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

Both tool lists are applied after the agent is fully assembled, so `task`, `send_message`
and MCP tools can be named. A name matching no tool refuses to start, with the list of
real ones.

### `[session]`

`dir` (a path; `./.stcode/sessions` keeps transcripts with the project) and `keep`, the
number of session files `prune` leaves behind.

### `[daemon]`

`transport` (`unix` / `tcp`), `socket`, `host`, `port`. See [daemon.md](daemon.md).

### `[mcp]`

`enabled`, and `expose` (`code` / `tools`). See [mcp.md](mcp.md).

### `[supervisor]`

`enabled`, `every`, `window`, `difficulty`. See [supervisor.md](supervisor.md).

### `[team]`

| Key | Default | |
| --- | --- | --- |
| `role` | `""` | **empty means solo.** Anything else must match a file in the agents directory |
| `shared_dir` | `/team` | the mounted volume |
| `max_agents` | `6` | |
| `wake_on_message` | `true` | start a turn when mail arrives and the agent is idle |
| `poll_interval` | `1.0` | seconds between inbox checks |
| `remote` | `/team/repo.git` | the bare origin. Point at a URL for real pull requests |
| `ssh_key` | `""` | only needed if `remote` is a URL. Mounted read-only, never copied |

### `[trace]`

`enabled`, `endpoint`, `headers`, `service_name`, `content`. Off by default; needs the
`otel` extra. See [tracing.md](tracing.md).

## Command-line overrides

```bash
stcode --mode auto-edit --model claude-sonnet-5 --role backend-dev --port 7717
```

`apply_cli_overrides` folds these into the loaded config **for this run only**. They are
never written back: a flag is a decision about now, and `--mode full-auto` on one run must
not leave `full-auto` in the file for the next.

`--role` is also how one image serves every role without a config file per container.

## Environment variables

| | |
| --- | --- |
| `STCODE_CONFIG` | where the config file is |
| `STCODE_AGENTS_DIR` | one directory of role profiles, and nothing else — what a container mounts |
| `STCODE_ROLE` | the role, same as `[team] role` |
| `STCODE_SANDBOX=1` | this is a contained environment; unlocks `full-auto` |
| `STCODE_MCP_CONFIG` | an `.mcp.json` somewhere other than the workspace |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY` | the conventional names |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | used when `[trace]` leaves them empty |
