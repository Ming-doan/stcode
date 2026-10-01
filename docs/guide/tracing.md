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
parent IDs and errors are mapped to each provider's JSON schema. Session
`usage` records contain the same `trace_id` so the dashboard and transcript can be joined.

`content = true` includes agent inputs/replies, model messages and tool inputs/results.
The default `false` leaves that content out. Error descriptions include only the
exception type when content recording is disabled.

A bounded background queue sends completed spans without blocking the agent loop.
Retryable HTTP failures and connection failures are attempted up to three times.
Langfuse requests with an ambiguous transport failure (such as a read timeout) are
not replayed: v4 does not guarantee deduplication. Partial rejections are not retried.
Permanent failures and queue overflow log warnings without failing the agent. Warnings never include keys or response bodies.
For embedded use, call `trace.shutdown()` at process exit (or
`await asyncio.to_thread(trace.shutdown)` from async code) to drain pending spans.
`trace.flush()` returns whether all exports since configuration succeeded; this confirms
HTTP ingestion, while dashboard visibility may follow shortly afterward.

## API compatibility

Langfuse uses `POST /api/public/otel/v1/traces` with OTLP/HTTP JSON, Basic
authentication, and `x-langfuse-ingestion-version: 4`. This selects Langfuse's
[real-time v4 ingestion path](https://langfuse.com/integrations/native/opentelemetry).
The existing `httpx` client sends the JSON directly; no collector or SDK is needed.
The application base URL and credential settings above remain the same.

Each turn has exactly one root `agent` observation, with `generation` and `tool`
children sharing its trace ID. Overall input/output live on the root. Trace name,
session ID and turn metadata are included on the children for v4 filtering. The
[observations table](https://langfuse.com/faq/all/explore-observations-in-v4) shows
all spans as rows; set **Is Root Observation = true** to see one row per turn and
open it to inspect the hierarchy. Legacy records already stored in Langfuse are
not rewritten by this change.

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
