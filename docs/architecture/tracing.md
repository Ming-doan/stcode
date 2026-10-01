# Tracing

`core/common/trace.py` exports the existing agent instrumentation using direct HTTP
JSON APIs. The session JSONL remains the trajectory; each usage record's `trace_id`
links it to the exported trace. See [configuration and API limits](../guide/tracing.md).

## Design

`configure(config.trace)` resolves the selected provider's URL and credentials from
process environment variables, falling back to TOML values. It never reads `.env`.
Configuration is process-wide and idempotent until shutdown. Missing Langfuse keys or
invalid URLs warn and leave tracing off. Disabled tracing starts no worker.

A context variable tracks the current span and propagates parentage through asyncio
tasks. IDs are random hex strings (32 characters for traces, 16 for spans). A turn is
a root span; model and tool spans inherit its trace ID, trace name, session ID and turn metadata. Each span
captures UTC start/end times, attributes and error status. Content is opt-in.

Completed spans enter a bounded queue. A background thread batches HTTP requests
through the existing `httpx` dependency. Langfuse retries connection failures and HTTP 429/502/503/504 responses with bounded
backoff. Ambiguous transport failures are not replayed because v4 does not guarantee
deduplication. Phoenix retains retries for transport errors and HTTP 429/5xx.
Langfuse's OTLP partialSuccess.rejectedSpans and Phoenix's queued count are checked
rather than treating every 2xx as success. Partial rejections are never retried.
Failures warn without propagating to the agent. `flush()` waits for a queue barrier;
`shutdown()` drains the queue and closes the HTTP client with a bounded wait.
Async callers use `asyncio.to_thread` for these blocking lifecycle operations.

## Wire mapping

| Internal operation | Langfuse | Phoenix |
| --- | --- | --- |
| Agent turn | OTLP span, type=agent | AGENT span |
| Model call | OTLP span, type=generation, model, usage_details | LLM span, llm.model_name, llm.token_count.* |
| Tool call | OTLP span, type=tool | TOOL span |
| Other | OTLP span, type=chain | CHAIN span |

Langfuse receives resourceSpans/scopeSpans with hex traceId/spanId/parentSpanId,
Unix nanosecond timestamps encoded as decimal strings, typed OTLP attributes, and
status. Requests use Basic authentication and x-langfuse-ingestion-version: 4.
The service name is a resource attribute; scope.name is stcode. Langfuse-specific
attributes map operation types, input/output, model, usage, session and filterable
metadata. Structured content and usage are JSON strings inside OTLP stringValue.
Each operation is exported when it ends; there is no separate trace-create event or
synthetic root. The same completed span is never intentionally exported as an update.

Phoenix sends `data` containing context, parent_id, span_kind, status_code,
start_time/end_time and OpenInference attributes.

## Verification

Contract tests inspect outgoing JSON and authentication, nested/concurrent parentage,
content opt-in, errors, retries, shutdown, environment precedence and secret redaction.
An agent integration test exercises the real gateway and tool path with a deterministic
model double. Live smoke tests explicitly opt into credentials and send a small trace,
then an actual agent request; Langfuse readback verifies exactly one root, parentage,
content, model and token counts through Observations API v2.
