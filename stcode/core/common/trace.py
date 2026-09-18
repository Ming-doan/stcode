"""
Tracing — the session, in a shape an observability platform already understands.

The session JSONL is the trajectory and stays the trajectory (rule 7: no second
logger). This is an *export*, not a second record: the same turns, calls and tool
invocations, emitted as OpenTelemetry spans while they happen.

    trace.configure(config.trace)                   # once, at startup. Off by default.
    with trace.span("chat claude-opus-5", kind="client", attributes={...}) as s:
        ...
        s.set(**{"gen_ai.usage.input_tokens": 1200})

Three decisions worth keeping:

* **OTLP, not a vendor SDK.** Langfuse, LangSmith, Phoenix and a plain Collector all
  ingest OTLP/HTTP; picking one platform's client would pick the platform.
* **`gen_ai.*` semantic conventions.** `chat <model>` spans with `gen_ai.request.model`
  and `gen_ai.usage.*` show up as *generations*, with token and cost accounting, in
  every one of those tools. Invented attribute names show up as neither.
* **The dependency is optional and imported late.** With `[trace] enabled = false`,
  which is the default, nothing here imports OpenTelemetry and every call is a few
  branches. Asked to trace without `stcode[otel]` installed, it says so once and stays
  off, rather than taking down a session that was about to do real work.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Mapping, Protocol

logger = logging.getLogger("stcode.trace")

INSTRUMENTATION_NAME = "stcode"

MISSING_DEPENDENCY = (
    "[trace] is enabled but OpenTelemetry is not installed, so nothing will be "
    "exported. Install it with `uv sync --extra otel`, or set [trace] enabled = false."
)

_tracer: Any = None
_provider: Any = None
_record_content = False


class Recorder(Protocol):
    """What a `span()` block can do to the span it is inside."""

    def set(self, **attributes: Any) -> None: ...

    def fail(self, exc: BaseException) -> None: ...


class _NoSpan:
    """The recorder handed out when tracing is off. Every method is a branch away."""

    __slots__ = ()

    def set(self, **attributes: Any) -> None:
        return None

    def fail(self, exc: BaseException) -> None:
        return None


class _RealSpan:
    """Thin adapter over an OTel span, so callers never import OpenTelemetry."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            if value is None:
                continue
            # Dots, not underscores: `gen_ai.usage.input_tokens` cannot be spelled as a
            # keyword argument, so callers pass it through `**{...}` and we undo the
            # substitution the ones that can get away with underscores rely on.
            self._span.set_attribute(key.replace("__", "."), value)

    def fail(self, exc: BaseException) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.record_exception(exc)
        self._span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


_NO_SPAN = _NoSpan()


def configure(settings: Any) -> bool:
    """Start exporting, or stay off. Idempotent — the second call is a no-op.

    `settings` is anything with `enabled`, `endpoint`, `headers`, `service_name` and
    `content`; `core/configs.py` owns the actual shape, and this module does not import
    it. Returns whether tracing is now on.
    """
    global _tracer, _provider, _record_content

    if _tracer is not None or not getattr(settings, "enabled", False):
        return _tracer is not None

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(MISSING_DEPENDENCY)
        return False

    endpoint = getattr(settings, "endpoint", "") or None
    headers = dict(getattr(settings, "headers", None) or {})
    resource = Resource.create({"service.name": getattr(settings, "service_name", "stcode")})

    # `endpoint=None` is not "no exporter": the exporter then reads
    # OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS itself, which is how
    # every platform's own documentation tells you to configure it.
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or None)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    _provider = provider
    _tracer = provider.get_tracer(INSTRUMENTATION_NAME)
    _record_content = bool(getattr(settings, "content", False))
    return True


def enabled() -> bool:
    return _tracer is not None


def records_content() -> bool:
    """Whether prompts and replies may go on a span.

    Off by default, and a separate switch from `enabled`: the shape of a run is not
    sensitive, the code in it usually is.
    """
    return _record_content


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: str = "internal",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Recorder]:
    """Record one operation. A no-op context manager when tracing is off.

    Made *current* for the block, so anything started inside it nests underneath —
    including work handed to `asyncio.gather`, which copies the context per task.
    """
    if _tracer is None:
        yield _NO_SPAN
        return

    from opentelemetry.trace import SpanKind

    span_kind = getattr(SpanKind, kind.upper(), SpanKind.INTERNAL)
    with _tracer.start_as_current_span(
        name, kind=span_kind, attributes=dict(attributes or {})
    ) as raw:
        recorder = _RealSpan(raw)
        try:
            yield recorder
        except BaseException as exc:
            recorder.fail(exc)
            raise


def current_ids() -> tuple[str, str]:
    """`(trace_id, span_id)` as hex, or two empty strings.

    What joins a session record to its span: the JSONL stays the trajectory, and this
    is the line from a line in it to the same moment in Langfuse.
    """
    if _tracer is None:
        return "", ""
    from opentelemetry import trace as otel_trace

    context = otel_trace.get_current_span().get_span_context()
    if not context.is_valid:
        return "", ""
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def shutdown() -> None:
    """Flush whatever the batch processor is holding. Called on the way out.

    Without it, the last turn of a short-lived run — exactly the one you were watching
    — is still in a buffer when the process exits.
    """
    global _tracer, _provider
    if _provider is not None:
        with contextlib.suppress(Exception):
            _provider.shutdown()
    _tracer, _provider = None, None


__all__ = [
    "INSTRUMENTATION_NAME",
    "MISSING_DEPENDENCY",
    "Recorder",
    "configure",
    "current_ids",
    "enabled",
    "records_content",
    "shutdown",
    "span",
]
