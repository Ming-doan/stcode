"""Export agent spans through Langfuse v4 OTLP/HTTP and Phoenix REST, without SDKs.

Callers keep the small span/Recorder interface. Context variables preserve asyncio
parentage; a bounded worker queue keeps HTTP I/O outside the agent loop.
"""
from __future__ import annotations

import contextlib
import logging
import json
import queue
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx

from stcode.core.configs import TraceConfig

logger = logging.getLogger("stcode.trace")
INSTRUMENTATION_NAME = "stcode"
SERVICE_NAME = "stcode"
_CONTENT_KEYS = {"stcode.input", "stcode.output", "gen_ai.input.messages", "gen_ai.output.messages"}
_TRACE_KEYS = {
    "gen_ai.conversation.id", "gen_ai.agent.name", "stcode.role", "stcode.approval_mode",
    "langfuse.user.id", "langfuse.session.id", "langfuse.version",
    "langfuse.release", "langfuse.environment",
}
_current: ContextVar[_Span | None] = ContextVar("stcode_span", default=None)
_exporter: _Exporter | None = None
_record_content = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder(Protocol):
    def set(self, **attributes: Any) -> None: ...
    def fail(self, exc: BaseException) -> None: ...


class _NoSpan:
    def set(self, **attributes: Any) -> None:
        pass

    def fail(self, exc: BaseException) -> None:
        pass


_NO_SPAN = _NoSpan()


@dataclass
class _Span:
    name: str
    trace_id: str
    parent_id: str | None
    content: bool
    trace_name: str = ""
    kind: str = "internal"
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    start: str = field(default_factory=_now)
    end: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def set(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            key = key.replace("__", ".")
            if value is not None and (self.content or key not in _CONTENT_KEYS):
                self.attributes[key] = value

    def fail(self, exc: BaseException) -> None:
        self.error = f"{type(exc).__name__}: {exc}" if self.content else type(exc).__name__

    @property
    def operation(self) -> str:
        op = self.attributes.get("gen_ai.operation.name")
        if op == "chat":
            return "LLM"
        if op == "execute_tool":
            return "TOOL"
        return "AGENT" if self.name.startswith("agent.turn ") else "CHAIN"


def _otel_value(value: Any) -> dict[str, Any]:
    """OTLP JSON AnyValue; structured attribute values are JSON strings."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_otel_value(item) for item in value]}}
    return {"stringValue": value if isinstance(value, str) else json.dumps(value)}


def _unix_nano(timestamp: str) -> str:
    # Avoid float timestamps: contemporary epoch nanoseconds exceed float precision.
    delta = datetime.fromisoformat(timestamp) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str(((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000)


def _langfuse(span: _Span) -> dict[str, Any]:
    original = span.attributes
    attrs = {k: v for k, v in original.items() if k not in _CONTENT_KEYS}
    attrs["langfuse.trace.name"] = span.trace_name
    attrs["langfuse.observation.type"] = {
        "AGENT": "agent", "LLM": "generation", "TOOL": "tool", "CHAIN": "chain",
    }[span.operation]
    if "gen_ai.conversation.id" in original:
        attrs["langfuse.session.id"] = original["gen_ai.conversation.id"]
    for key, value in original.items():
        if key in _CONTENT_KEYS:
            continue
        if key.startswith("stcode.") or key == "gen_ai.agent.name":
            level = "trace" if key in _TRACE_KEYS else "observation"
            attrs[f"langfuse.{level}.metadata.{key.replace('.', '_')}"] = (
                value if isinstance(value, str) else json.dumps(value)
            )
    for target, keys in (("input", ("stcode.input", "gen_ai.input.messages")),
                         ("output", ("stcode.output", "gen_ai.output.messages"))):
        for key in keys:
            if key in original:
                value = original[key]
                attrs[f"langfuse.observation.{target}"] = (
                    value if isinstance(value, str) else json.dumps(value)
                )
                break
    if span.operation == "LLM":
        if "gen_ai.request.model" in original:
            attrs["langfuse.observation.model.name"] = original["gen_ai.request.model"]
        usage = {
            target: original[source] for source, target in (
                ("gen_ai.usage.input_tokens", "input"),
                ("gen_ai.usage.output_tokens", "output"),
            ) if source in original
        }
        attrs["langfuse.observation.usage_details"] = json.dumps(usage)
    body: dict[str, Any] = {
        "traceId": span.trace_id, "spanId": span.span_id, "name": span.name,
        "kind": {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}.get(span.kind, 1),
        "startTimeUnixNano": _unix_nano(span.start), "endTimeUnixNano": _unix_nano(span.end),
        "attributes": [{"key": k, "value": _otel_value(v)} for k, v in attrs.items()],
        "status": {"code": 2, "message": span.error} if span.error else {"code": 1},
    }
    if span.parent_id:
        body["parentSpanId"] = span.parent_id
    return body


def _phoenix(span: _Span) -> dict[str, Any]:
    attrs = {k: v for k, v in span.attributes.items() if k not in _CONTENT_KEYS}
    mapping = {
        "gen_ai.request.model": "llm.model_name",
        "gen_ai.provider.name": "llm.provider",
        "gen_ai.usage.input_tokens": "llm.token_count.prompt",
        "gen_ai.usage.output_tokens": "llm.token_count.completion",
        "gen_ai.usage.cache_read_input_tokens": "llm.token_count.prompt_details.cache_read",
        "gen_ai.conversation.id": "session.id",
        "gen_ai.tool.name": "tool.name",
    }
    for source, target in mapping.items():
        if source in attrs:
            attrs[target] = attrs[source]
    for target, keys in (("input", ("stcode.input", "gen_ai.input.messages")),
                         ("output", ("stcode.output", "gen_ai.output.messages"))):
        for key in keys:
            if key in span.attributes:
                value = span.attributes[key]
                attrs[f"{target}.value"] = value if isinstance(value, str) else json.dumps(value)
                attrs[f"{target}.mime_type"] = "text/plain" if isinstance(value, str) else "application/json"
                break
    return {
        "name": span.name, "context": {"trace_id": span.trace_id, "span_id": span.span_id},
        "parent_id": span.parent_id, "span_kind": span.operation,
        "start_time": span.start, "end_time": span.end,
        "status_code": "ERROR" if span.error else "OK", "status_message": span.error,
        "attributes": attrs,
    }


@dataclass
class _Barrier:
    ready: threading.Event = field(default_factory=threading.Event)
    ok: bool = False


class _Exporter:
    def __init__(self, provider: str, url: str, headers: dict[str, str], service: str) -> None:
        self.provider, self.url, self.headers, self.service = provider, url, headers, service
        self.queue: queue.Queue[_Span | _Barrier] = queue.Queue(maxsize=2048)
        self.stopping = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="stcode-trace", daemon=True)
        self.thread.start()

    def submit(self, span: _Span) -> None:
        try:
            self.queue.put_nowait(span)
        except queue.Full:
            self.failed.set()
            logger.warning("Trace export queue full; span dropped")

    def payload(self, spans: list[_Span]) -> dict[str, Any]:
        if self.provider == "phoenix":
            return {"data": [_phoenix(s) for s in spans]}
        return {"resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": self.service}}]},
            "scopeSpans": [{"scope": {"name": INSTRUMENTATION_NAME},
                            "spans": [_langfuse(span) for span in spans]}],
        }]}

    def _send(self, client: httpx.Client, spans: list[_Span]) -> None:
        # Build completed spans once. V4 does not guarantee deduplication on replay.
        payload = self.payload(spans)
        for attempt in range(3):
            try:
                response = client.post(self.url, headers=self.headers, json=payload)
                retryable = response.status_code in (429, 502, 503, 504)
                if self.provider == "phoenix" and response.status_code >= 500:
                    retryable = True
                if retryable:
                    if attempt < 2:
                        time.sleep(0.2 * 2 ** attempt)
                        continue
                response.raise_for_status()
                result = response.json() if response.content else {}
                if self.provider == "langfuse":
                    if response.status_code != 200:
                        raise ValueError("unexpected OTLP response status")
                    partial = result.get("partialSuccess", {})
                    if int(partial.get("rejectedSpans", 0)):
                        raise ValueError("OTLP ingestion rejected spans")
                    if partial.get("errorMessage"):
                        logger.warning("Langfuse accepted trace export with an ingestion warning")
                elif result.get("total_queued") != len(spans):
                    raise ValueError("ingestion did not queue all spans")
                return
            except httpx.TransportError as exc:
                # A read/write failure may follow a successful ingestion. Replaying
                # that request can create duplicate observations on the v4 read path.
                if self.provider == "langfuse" and not isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                ):
                    raise
                if attempt < 2:
                    time.sleep(0.2 * 2 ** attempt)
                    continue
                raise

    def _run(self) -> None:
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        item = self.queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if isinstance(item, _Barrier):
                        item.ok = not self.failed.is_set()
                        item.ready.set()
                        continue
                    spans = [item]
                    barrier = None
                    while len(spans) < 64:
                        try:
                            next_item = self.queue.get_nowait()
                        except queue.Empty:
                            break
                        if isinstance(next_item, _Barrier):
                            barrier = next_item
                            break
                        spans.append(next_item)
                    try:
                        self._send(client, spans)
                    except Exception as exc:
                        self.failed.set()
                        # Response bodies, URLs and exception messages may contain secrets.
                        logger.warning("%s trace export failed (%s)", self.provider, type(exc).__name__)
                    if barrier is not None:
                        barrier.ok = not self.failed.is_set()
                        barrier.ready.set()
        except Exception as exc:
            self.failed.set()
            logger.warning("Trace export worker stopped (%s)", type(exc).__name__)

    def flush(self, timeout: float) -> bool:
        barrier = _Barrier()
        started = time.monotonic()
        try:
            self.queue.put(barrier, timeout=timeout)
        except queue.Full:
            return False
        return barrier.ready.wait(max(0, timeout - (time.monotonic() - started))) and barrier.ok


def configure(settings: TraceConfig) -> bool:
    """Start one exporter from `[trace]`. Credentials are resolved by the config."""
    global _exporter, _record_content
    if _exporter is not None or not settings.enabled:
        return _exporter is not None
    values = settings.resolved()
    url = values["url"]
    headers = {}
    if settings.provider == "langfuse":
        public, secret = values["public_key"], values["secret_key"]
        if not public or not secret:
            logger.warning("Langfuse trace credentials are missing; tracing disabled")
            return False
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()
        headers["x-langfuse-ingestion-version"] = "4"
        headers["Accept"] = "application/json"
        path = "/api/public/otel/v1/traces"
    else:
        project = values["project_name"]
        if any(c in project for c in "/?#"):
            logger.warning("Invalid Phoenix trace project name; tracing disabled")
            return False
        if values["api_key"]:
            headers["Authorization"] = f"Bearer {values['api_key']}"
        path = f"/v1/projects/{quote(project, safe='')}/spans"
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("invalid base URL")
        if parsed.path.rstrip("/").endswith(("/v1/traces", "/api/public/ingestion")):
            raise ValueError("expected application base URL")
    except ValueError:
        logger.warning("Invalid trace base URL; tracing disabled")
        return False
    _exporter = _Exporter(settings.provider, url.rstrip("/") + path, headers, SERVICE_NAME)
    _record_content = bool(settings.content)
    return True


def enabled() -> bool:
    return _exporter is not None


def records_content() -> bool:
    return _record_content


@contextlib.contextmanager
def span(name: str, *, kind: str = "internal", attributes: Mapping[str, Any] | None = None) -> Iterator[Recorder]:
    """Record an operation, inheriting the current task's parent. Off is a no-op."""
    exporter = _exporter
    if exporter is None:
        yield _NO_SPAN
        return
    parent = _current.get()
    recorder = _Span(name, parent.trace_id if parent else secrets.token_hex(16),
                     parent.span_id if parent else None, _record_content,
                     trace_name=parent.trace_name if parent else name, kind=kind.lower())
    recorder.set(**{"service.name": exporter.service})
    if parent:
        recorder.set(**{k: v for k, v in parent.attributes.items()
                        if k in _TRACE_KEYS or k.startswith("langfuse.trace.")})
    recorder.set(**dict(attributes or {}))
    token = _current.set(recorder)
    try:
        yield recorder
    except GeneratorExit:
        # Agent.run closes its generator after a terminal event; this is normal.
        raise
    except BaseException as exc:
        recorder.fail(exc)
        raise
    finally:
        recorder.end = _now()
        _current.reset(token)
        exporter.submit(recorder)


def current_ids() -> tuple[str, str]:
    current = _current.get() if enabled() else None
    return (current.trace_id, current.span_id) if current else ("", "")


def flush(timeout: float = 30) -> bool:
    """Wait for queued exports. False means a timeout or any export failure since configure."""
    return _exporter.flush(timeout) if _exporter else True


def shutdown(timeout: float = 30) -> None:
    """Drain queued exports and close the client. Async callers should use to_thread."""
    global _exporter, _record_content
    exporter, _exporter = _exporter, None
    _record_content = False
    if exporter is not None:
        exporter.stopping.set()
        exporter.thread.join(timeout)
        if exporter.thread.is_alive():
            logger.warning("Trace export shutdown timed out; pending spans may be lost")


__all__ = ["Recorder", "configure", "current_ids", "enabled", "records_content", "span", "flush", "shutdown"]
