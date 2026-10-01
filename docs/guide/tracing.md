# Tracing

Send agent turns, model calls and tool calls directly to Langfuse or Phoenix using
HTTP JSON APIs. No OpenTelemetry collector, exporter, or provider SDK is required.
Tracing is off by default.

```toml
[trace]
enabled = true
provider = "langfuse"
url = "https://cloud.langfuse.com"
# public_key = "pk-lf-..."
# secret_key = "sk-lf-..."
content = false
```

Or use Phoenix (the project must already exist):

```toml
[trace]
enabled = true
provider = "phoenix"
url = "http://localhost:6006"
project_name = "stcode"
# api_key = "..."  # omit for a local server without authentication
content = false
```

`url` is the application base URL, including any deployment path prefix, without an
ingestion path. Credentials can be literal values in `config.toml` or process
environment variables. Nonempty environment values take precedence over config:

| Provider | Environment variables |
| --- | --- |
| Langfuse | `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`), `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` |
| Phoenix | `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_API_KEY`, `PHOENIX_PROJECT_NAME` |

The default URLs are `https://cloud.langfuse.com` and `http://localhost:6006`.
Phoenix defaults to project `default`. `service_name` defaults to `stcode`.
The application never loads `.env` files. Export variables in the launching process
or put values in `config.toml`. Test runners may explicitly read test credentials.

## What you get

Each `agent.turn <name>` is a trace containing `chat <model>` generations and
`execute_tool <name>` spans. Model names, input/output token counts, timestamps,
parent IDs and errors are mapped to each provider's native JSON schema. Session
`usage` records contain the same `trace_id` so the dashboard and transcript can be joined.

`content = true` includes agent inputs/replies, model messages and tool inputs/results.
The default `false` leaves that content out. Error descriptions include only the
exception type when content recording is disabled.

A bounded background queue sends completed spans without blocking the agent loop.
Transient HTTP failures are retried three times; permanent failures and queue overflow
log warnings without failing the agent. Warnings never include keys or response bodies.
For embedded use, call `trace.shutdown()` at process exit (or
`await asyncio.to_thread(trace.shutdown)` from async code) to drain pending spans.
`trace.flush()` returns whether all exports since configuration succeeded; this confirms
HTTP ingestion, while dashboard visibility may follow later (several minutes for
Langfuse native ingestion).

## API compatibility

Langfuse uses `POST /api/public/ingestion` with Basic authentication and
`trace-create`, `span-create`, and `generation-create` events. This native API is
[deprecated by Langfuse](https://langfuse.com/faq/all/deprecated-api-migration): Cloud
support is scheduled to end November 16, 2026. Use a deployment that still supports
this endpoint. Its replacement uses OTLP, which this integration does not implement.

Phoenix uses `POST /v1/projects/{project_name}/spans` with Bearer authentication and
native JSON spans ([server API contract](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/server/api/routers/v1/spans.py)).
The server must provide this REST endpoint and the project must exist.
Old `[trace] endpoint/headers` OTLP settings must be replaced with the provider
configuration above.

→ [Tracing (architecture)](../architecture/tracing.md)

## Live acceptance test

Export the variables above plus `OPENAI_API_KEY`, optional `OPENAI_BASE_URL`, and
`STCODE_TRACE_TEST_MODEL` (a tool-capable model served by that endpoint). Run:

```bash
uv run pytest -m live tests/integration/trace_live_test.py -s
```

This makes an agent read a synthetic file in a temporary workspace and reply with its
contents. Each provider's trace ID and session ID are printed for dashboard review.
The test deliberately enables content recording and never reads `.env` itself.
