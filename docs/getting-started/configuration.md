# Configuration

Use `/model`, `/mode`, and `/effort` in the terminal to change your setup.
For a reusable configuration, edit `~/.stcode/config.toml`.
Run `stcode config` or open the `?` help card to find the active configuration.

Set `STCODE_HOME` to choose a different settings directory, or `STCODE_CONFIG` to
choose a configuration file. You can also start with `stcode --config path/to/config.toml`.
If a local service is already running, use `--restart` to load a different file;
this interrupts its active work.

## Providers and credentials

A minimal OpenAI configuration:

```toml
[model.providers.openai]
provider = "openai"
api_key = "${OPENAI_API_KEY}"

[agent]
approval_mode = "suggest"
reasoning_effort = "medium"
```

| Provider | `provider` value | Default credential variable |
| --- | --- | --- |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |
| Google Gemini | `google` | `GEMINI_API_KEY` |

Add `model = "your-model-id"` to choose a model available from that provider.
When omitted, stcode uses the provider adapter's default. `/model` shows the active
selection.

An `api_key` can contain a literal key or a whole-value reference such as
`"${WORK_API_KEY}"`. A configured key takes precedence; the provider's default
environment variable is the fallback. Export variables in the process that starts
stcode; `.env` files are not loaded automatically. Keep credentials out of version control.

## Compatible endpoints

Set `base_url` on an OpenAI provider entry for an endpoint that supports the API
used by stcode. Model and tool support depend on that endpoint.

```toml
[model.providers.local]
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "your-served-model"
api_key = "${LOCAL_MODEL_KEY}"
```

## Multiple accounts and models

Provider entry names are yours to choose. Set `[model] default` to the entry you
want to use. Optional routing entries choose a different model for a difficulty tier.

```toml
[model]
default = "work"

[model.providers.work]
provider = "openai"
api_key = "${WORK_OPENAI_KEY}"

[model.providers.personal]
provider = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"

[model.routing.high]
provider = "personal"
model = "your-anthropic-model-id"
```

Use `/model` to review or edit routing for `low`, `medium`, and `high` tasks.

## Preferences and permissions

`/theme` selects automatic, dark, or light appearance. Theme and trusted-folder
preferences are saved in `~/.stcode/ui.toml`.

Use `/mode` for [approval modes](../guide/approval-modes.md), or pass a mode when
opening a session:

```bash
stcode ~/projects/api --mode auto-edit
```

Command-line overrides apply to that run. Changes made through the settings UI are
saved. When connected remotely, model and agent settings apply to the connected
service; appearance settings apply to your terminal.

## Optional features

Enable the supervisor to help the agent reconsider work that appears stuck:

```toml
[supervisor]
enabled = true
```

For other integrations, see [MCP servers](../guide/mcp.md),
[skills](../guide/skills.md), [tracing](../guide/tracing.md), and [team profiles](../teams/roles.md).
