# Configuration

`core/configs.py` is the one place a setting is located, read, resolved and written. It
imports nothing from `stcode`, so every layer of `core/` may import it. No other module
parses TOML or reads the environment for a setting: each is handed the slice it needs
(`Agent.create` the whole `Config`, `LLMGateway` its `ModelConfig`, `trace.configure`
its `TraceConfig`).

The file belongs to the **daemon**. The UI never reads or writes it; it asks the daemon
over the socket (`get_config` / `set_config`). → [daemon.md](daemon.md#configuration-over-the-socket)

```
~/.stcode/config.toml                  # or %APPDATA%\stcode\config.toml on Windows
$STCODE_HOME/config.toml               # STCODE_HOME moves the whole stcode directory
$STCODE_CONFIG                         # names the file itself — what a container sets
stcode-daemon --config PATH            # the same, for one process
```

The daemon scaffolds a minimal file on first start (`DEFAULT_CONFIG`), mode 0600.

## Reading, resolving, writing

| | Function | |
| --- | --- | --- |
| Read | `load_config(path, cwd, overrides=…)` | TOML → validated `Config`. `overrides` are dotted keys for this process only |
| Resolve | `secret(value)` | a literal, or a whole-value `"${ENV_VAR}"` reference read now |
| Write | `update_config(path, {"agent.approval_mode": "plan"})` | patches those keys in the document; `None` removes one |

**Writes patch, they do not dump.** `update_config` edits the parsed TOML document
(`tomlkit`) and writes it back atomically, so comments and untouched keys survive, and
nothing that came from the environment — a resolved key, `STCODE_TEAM=1` — can reach the
file. A patch whose result would not validate is refused before anything is written: the
daemon must never write the file it will fail to start from.

**Flags are overrides, not edits.** `stcode-daemon --transport tcp --mode plan` becomes
`{"daemon.transport": "tcp", "agent.approval_mode": "plan"}`, applied at every load —
including the reload after a `set_config` — and never written back.

**An unknown top-level section is an error.** A file in the old layout (`[defaults]`,
`[providers.*]`, `[routing.*]`) says so at load instead of starting with no provider.

## Secrets

```toml
[model.providers.openai]
api_key = "${OPENAI_API_KEY}"      # a reference: the file never holds the key
# api_key = "sk-…"                 # or a literal — what the UI writes when you paste one
```

A reference is resolved when the value is used, so changing the variable and reloading
is enough. An unset variable reads as no key. Where a key is unset altogether, the
library's conventional variable is used (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `TAVILY_API_KEY`, the Langfuse and Phoenix ones). `.env` is never loaded.

## What is *not* in this file

The TUI's own state — the theme, the folders you have trusted, and how long a `!`
command may run — is in `~/.stcode/ui.toml`, read by the terminal client and by nothing
else. A container has no theme, trusts nothing, and never runs a `!`.
→ [decision 0003](../decisions/0003-what-the-tui-owns.md)

```toml
# ~/.stcode/ui.toml
theme         = "auto"        # auto | dark | light
trusted       = ["/home/you/work/stcode"]
shell_timeout = 10            # seconds a `!` command may run. Clamped to 1–120
```

## Every section

### `[model]` — providers, routing, retry

```toml
[model]
default = "work"                     # the entry used when no tier applies; unset = the first

[model.providers.work]               # the name is yours; two accounts = two entries
provider       = "openai"            # the library: openai | anthropic | google
api_key        = "${OPENAI_API_KEY}"
model          = "gpt-5"             # unset = the library's default model
base_url       = "${OPENAI_BASE_URL}"
max_concurrent = 0                   # completions this endpoint serves at once

[model.routing.low]                  # optional, per difficulty tier
provider = "work"                    # an entry name above
model    = "gpt-5-mini"

[model.retry]
max_attempts = 3                     # base_delay = 1.0, max_delay = 20.0, jitter = true
```

A tier resolves to its `[model.routing.<tier>]` line if there is one, else to the
default entry and its `model`. Naming an entry that does not exist is a load error.

`max_concurrent` caps completions in flight against **this endpoint**, across every
session, sub-agent and the supervisor, because one daemon shares one gateway. `0` is no
cap, right for a hosted API; `1` for Ollama or llama.cpp. → [providers.md](providers.md)

### `[agent]` — defaults and limits on one turn

| Key | Default | |
| --- | --- | --- |
| `reasoning_effort` | `"medium"` | `none` … `max`. What `/effort` writes |
| `approval_mode` | `"suggest"` | default for new sessions. What `/mode` writes; `full-auto` only in a container |
| `max_turns` | `40` | tool-call ceiling **within** one turn, not a conversation length |
| `max_depth` | `1` | not meant to be raised: recursion with no budget is a fork bomb |
| `max_concurrent` | `4` | tools running at once |
| `enable_task` | `true` | whether sub-agents exist |
| `difficulty` | `"high"` | the tier the main loop uses |
| `tools` | `[]` | an allow-list **replacing** the default set. Empty means the default |
| `exclude_tools` | `[]` | subtracted from whatever is left |
| `prompt` | `""` | this agent's own prompt, inline. Sits high in the cached prefix |
| `prompt_file` | unset | the same thing in a file, **relative to this config file**. `prompt` wins if both are set |

Both tool lists are applied after the agent is fully assembled, so `task`, the team tools
and MCP tools can be named. A name matching no tool refuses to start.

`prompt` and `prompt_file` are what make one config file a whole agent — see
[agent profiles](harness.md#an-agent-profile-is-one-configtoml).

### `[session]`

`dir` (default `~/.stcode/sessions`; `./.stcode/sessions` keeps transcripts with the
project) and `keep`, the number of session files `prune` leaves behind.

### `[daemon]`

| Key | Default | |
| --- | --- | --- |
| `transport` | `"unix"` | `unix` on your machine, `tcp` in a container |
| `socket` | `~/.stcode/daemon.sock` | `unix` only |
| `host` / `port` | `127.0.0.1` / `7717` | `tcp` only |
| `max_clients` | *unset* | concurrent connections. **Unset = 1 in a container, unlimited on the host**; `0` is unlimited everywhere |

Read by the daemon only. The UI connects to `~/.stcode/daemon.sock` unless given
`--socket` or `--transport tcp --host --port`, and starts the daemon on that same
address. → [daemon.md](daemon.md#one-client-in-a-container)

### `[mcp]`

`enabled`, and `expose` (`code` / `tools`). See [mcp.md](mcp.md).

### `[supervisor]`

`enabled` (default off; `STCODE_SUPERVISOR=1` turns it on), `every`, `window`,
`difficulty`. See [supervisor.md](supervisor.md).

### `[team]`

| Key | Default | |
| --- | --- | --- |
| `enabled` | `false` | **the switch.** Also `STCODE_TEAM=1` |
| `role` | `""` | which agent this is. Also `STCODE_ROLE` |
| `name` | `""` | the team this agent belongs to; written on its member card |
| `description` | `""` | one line on what this agent does — what `find_teammate` shows the others |
| `shared_dir` | `~/.stcode/team` | the mounted volume; `/team` in a container |
| `url` | `""` | **reserved** for a team service. Setting it refuses to start until one exists |
| `wake_on_message` | `true` | start a turn when mail arrives and the agent is idle |
| `poll_interval` | `1.0` | seconds between inbox checks |

`enabled` and `role` are two facts. A role says *which agent this is*, and is useful
in a solo session too. Team mode says *a shared volume is mounted*. Naming a role does
not imply a team.

`name`, `description` and `url` are the shape a team service will need: which team,
what this member is for, and where to reach the service instead of a shared disk.
→ [Room for a team service](team.md#room-for-a-team-service)

### `[trace]`

`enabled`, `provider` (`langfuse` or `phoenix`), `url`, `public_key`, `secret_key`,
`api_key`, `project_name`, `content`. Off by default. Unset values fall back to the
provider's own environment variables. See [tracing.md](tracing.md).

### `[tools]`

`tavily_api_key` for `web_search`, default `$TAVILY_API_KEY`.

## Environment variables

| | |
| --- | --- |
| `STCODE_HOME` | the stcode directory: config, sessions, socket, `ui.toml`, `daemon.log` |
| `STCODE_CONFIG` | the config file itself |
| `STCODE_ROLE` | the role, same as `[team] role` |
| `STCODE_TEAM=1` | turn team mode on, same as `[team] enabled` |
| `STCODE_SUPERVISOR=1` | turn the supervisor on |
| `STCODE_SANDBOX=1` | this is a contained environment; unlocks `full-auto`. Deliberately **not** a config key — a file must not be able to unlock rule 5 |
| `STCODE_MCP_CONFIG` | an `.mcp.json` somewhere other than the workspace |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `TAVILY_API_KEY` | conventional fallbacks for unset keys |
| `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse fallbacks |
| `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY`, `PHOENIX_PROJECT_NAME` | Phoenix fallbacks |
