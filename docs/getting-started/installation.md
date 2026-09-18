# Installation

## Requirements

| | |
| --- | --- |
| Python | 3.12 or newer |
| [uv](https://docs.astral.sh/uv/) | used for everything — running, locking, syncing |
| Docker | only for [team mode](../teams/setup.md) |

`ripgrep` is **not** a system requirement. It arrives as a pip wheel that drops `rg`
next to the interpreter, so `grep` works on a fresh checkout with nothing installed
system-wide. `fd` is used if present and skipped if not.

## Install

```bash
git clone https://github.com/minhdoan/stcode
cd stcode
uv sync
uv run stcode
```

### Optional extras

```bash
uv sync --extra otel      # OpenTelemetry export — see Tracing
uv sync --group docs      # build this documentation site
```

Tracing is an extra rather than a dependency on purpose: a coding session that is not
being watched should not pay for a tracer. → [Tracing](../guide/tracing.md)

## Credentials

stcode reads an API key from the environment or from its config file, and the
environment wins whenever it is set.

```bash
export ANTHROPIC_API_KEY=sk-ant-…
# or OPENAI_API_KEY, or GEMINI_API_KEY
```

`.env` files are loaded too, most specific first: `./.env`, then `~/.stcode/.env`. Real
process environment variables beat both.

If you would rather not manage environment variables, the first-run setup screen will
take a key and write it into `~/.stcode/config.toml`, which is created mode `0600` for
that reason.

## Check it worked

```bash
uv run stcode config     # where config lives and what it selects
uv run pytest            # 240-odd tests, no network
```

The test suite does not call a real provider. The handful of tests that do are marked
`live` and deselected by default. → [Testing](../contributing/testing.md)
