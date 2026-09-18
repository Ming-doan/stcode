# Tracing

Export the trajectory as OpenTelemetry spans, for Langfuse, LangSmith, Phoenix, or a
plain OTLP collector.

**Off by default**, and the dependency is an extra: a coding session that is not being
watched should not pay for a tracer.

```bash
uv sync --extra otel
```

```toml
[trace]
enabled  = true
endpoint = "https://cloud.langfuse.com/api/public/otel/v1/traces"
headers  = { Authorization = "Basic <base64 of public:secret>" }
content  = false
```

Leave `endpoint` and `headers` empty to configure it the way every platform's own docs
tell you to, through `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`.

## What you get

| Span | When |
| --- | --- |
| `agent.turn <name>` | one thing the agent was asked to do — everything nests under it |
| `chat <model>` | one model call, with `gen_ai.usage.*` token counts |
| `execute_tool <name>` | one tool invocation |

The names and attributes follow the **`gen_ai.*` semantic conventions**, which is why
these show up in Langfuse as *generations* with token and cost accounting rather than
as anonymous boxes. Invented attribute names show up as neither.

## `content` is a separate, louder switch

```toml
content = false   # the default
```

The *shape* of a run is not sensitive. The code in it usually is. Turning `content` on
puts prompts and file contents on spans; leave it off unless you know where those spans
are going.

## It is an export, not a second record

The session JSONL is the trajectory and stays the trajectory. Every model call already
lands there as a `usage` record — one place, one writer. Tracing reads the same events
as they happen and emits them in a shape a platform understands.

The two are joined by a `trace_id` written into the session record when tracing is on,
so a line in the JSONL points at the same moment in Langfuse. With tracing off the
field is absent and the session stands alone exactly as before.

## If the extra is not installed

Enabling `[trace]` without `stcode[otel]` **warns once and stays off**. A session that
was about to do real work should not be taken down because its observability
dependency is missing.

→ [Tracing (architecture)](../architecture/tracing.md)
