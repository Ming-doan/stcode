# Tracing

`core/common/trace.py` exports the existing agent instrumentation using native HTTP
JSON APIs. The session JSONL remains the trajectory; each usage record's `trace_id`
links it to the exported trace. See [configuration and API limits](../guide/tracing.md).

## Design

`configure(config.trace)` resolves the selected provider's URL and credentials from
process environment variables, falling back to TOML values. It never reads `.env`.
Configuration is process-wide and idempotent until shutdown. Missing Langfuse keys or
invalid URLs warn and leave tracing off. Disabled tracing starts no worker.

A context variable tracks the current span and propagates parentage through asyncio
tasks. IDs are random hex strings (32 characters for traces, 16 for spans). A turn is
a root span; model and tool spans inherit its trace ID and session ID. Each span
captures UTC start/end times, attributes and error status. Content is opt-in.

Completed spans enter a bounded queue. A background thread batches HTTP requests
through the existing `httpx` dependency. It retries transport errors, HTTP 429 and
5xx responses, with a bounded timeout and backoff. Langfuse's HTTP 207 per-event
errors and Phoenix's queued count are checked rather than treating every 2xx as success.
Failures warn without propagating to the agent. `flush()` waits for a queue barrier;
`shutdown()` drains the queue and closes the HTTP client with a bounded wait.
Async callers use `asyncio.to_thread` for these blocking lifecycle operations.

## Wire mapping

| Internal operation | Langfuse | Phoenix |
| --- | --- | --- |
| Agent turn | trace-create plus root span-create | AGENT span |
| Model call | generation-create, model, usageDetails | LLM span, llm.model_name, llm.token_count.* |
| Tool call | span-create | TOOL span |
| Other | span-create | CHAIN span |

Langfuse events carry unique event IDs and ISO timestamps; observations carry
traceId, parentObservationId, startTime and endTime. Root events carry sessionId.
Phoenix sends `data` containing context, parent_id, span_kind, status_code,
start_time/end_time and OpenInference attributes. No OTLP envelopes are emitted.

## Verification

Contract tests inspect outgoing JSON and authentication, nested/concurrent parentage,
content opt-in, errors, retries, shutdown, environment precedence and secret redaction.
An agent integration test exercises the real gateway and tool path with a deterministic
model double. Live smoke tests explicitly opt into credentials and send a small trace,
then an actual agent request; record trace IDs for dashboard acceptance.
