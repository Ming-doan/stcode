# Tracing

`core/common/trace.py`. The trajectory, in a shape an observability platform already
understands. **Off by default.**

The session JSONL is the trajectory and stays the trajectory — there is no second logger.
This is an *export*: the same turns, model calls and tool invocations, emitted as
OpenTelemetry spans while they happen.

## Turning it on

```bash
uv sync --extra otel
```

```toml
[trace]
enabled  = true
endpoint = "https://cloud.langfuse.com/api/public/otel/v1/traces"
headers  = { Authorization = "Basic <base64 of public:secret>" }
service_name = "stcode"
content  = false
```

Leave `endpoint` and `headers` empty to configure it the way every platform's own
documentation does, through `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`.

| Platform | Endpoint |
| --- | --- |
| Langfuse | `https://cloud.langfuse.com/api/public/otel/v1/traces` (Basic auth from the project keys; `us.` and `jp.` for other regions) |
| LangSmith | `https://api.smith.langchain.com/otel/v1/traces` (`x-api-key`) |
| Phoenix / Jaeger / a Collector | wherever it listens for OTLP-HTTP |

Enabled without the extra installed, it logs one warning and stays off. A session that was
about to do real work should not die because the tracer is missing.

## The spans

| Span | Kind | Key attributes |
| --- | --- | --- |
| `agent.turn <name>` | internal | `gen_ai.agent.name`, `gen_ai.conversation.id` (the session id), `stcode.role`, `stcode.approval_mode` |
| `chat <model>` | client | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.request.max_tokens`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read_input_tokens`, `gen_ai.response.finish_reasons`, `stcode.difficulty` |
| `execute_tool <name>` | internal | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `stcode.agent`, `stcode.tool.ok` |

One trace is one turn. Model calls and tool calls nest inside it — including tools handed
to `asyncio.gather`, because a task copies the current context when it is created.

**The names and attributes are the GenAI semantic convention, not ours.** A `chat <model>`
span with `gen_ai.*` attributes shows up in Langfuse, LangSmith and Phoenix as a
*generation*, with token counts and therefore cost. The same information under invented
names shows up as an anonymous box.

## Two decisions

**OTLP, not a vendor SDK.** Every one of those platforms ingests OTLP over HTTP; picking
one platform's client would pick the platform. The one dependency is the standard
exporter.

**`content` is a separate, louder switch.** The *shape* of a run — how many turns, which
tools, how many tokens — is not sensitive. The prompts and file contents inside it usually
are. Turning on tracing must not turn on sending your code to a third party.

## Joining a span back to the transcript

Every `usage` record carries the `trace_id` of the call that produced it, when tracing is
on:

```jsonl
{"type":"usage","difficulty":"high","stop_reason":"tool_use","trace_id":"4bf92f…","input_tokens":12043,"output_tokens":881}
```

so a line in the JSONL and a span in Langfuse are the same moment, reachable from either
end. With tracing off the field is absent and the session stands alone exactly as before.

## Cost when it is off

Nothing imports OpenTelemetry. `span()` yields a shared no-op recorder, `current_ids()`
returns two empty strings, and `configure()` returns `False` without touching the
filesystem or the network. The instrumented call sites are a `None` check each.

## Using it from your own code

```py
from stcode.core.common import trace

with trace.span("my.operation", attributes={"thing": "value"}) as recorder:
    recorder.set(**{"gen_ai.usage.input_tokens": 1200})
```

`configure()` is idempotent and is called by `Agent.create`, so the first agent built in a
process starts the exporter and every later one finds it running. `trace.shutdown()`
flushes what the batch processor is holding; the daemon calls it last on the way out,
because without it the final turn of a short run — the one you were watching — dies in a
buffer.
