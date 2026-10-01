"""Export agent spans through Langfuse/Phoenix native HTTP APIs, without SDKs.

Callers keep the small span/Recorder interface. Context variables preserve asyncio
parentage; a bounded worker queue keeps HTTP I/O outside the agent loop.
"""
from __future__ import annotations

import contextlib
import logging
import os
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

logger = logging.getLogger("stcode.trace")
INSTRUMENTATION_NAME = "stcode"
_CONTENT_KEYS = {"stcode.input", "stcode.output", "gen_ai.input.messages", "gen_ai.output.messages"}
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


def _langfuse(span: _Span) -> dict[str, Any]:
    attrs = span.attributes
    body: dict[str, Any] = {
        "id": span.span_id, "traceId": span.trace_id, "name": span.name,
        "startTime": span.start, "endTime": span.end,
        "metadata": {k: v for k, v in attrs.items() if k not in _CONTENT_KEYS},
    }
    if span.parent_id:
        body["parentObservationId"] = span.parent_id
    if span.error:
        body.update(level="ERROR", statusMessage=span.error)
    for target, keys in (("input", ("stcode.input", "gen_ai.input.messages")),
                         ("output", ("stcode.output", "gen_ai.output.messages"))):
        for key in keys:
            if key in attrs:
                body[target] = attrs[key]
                break
    if span.operation == "LLM":
        body["model"] = attrs.get("gen_ai.request.model")
        body["usageDetails"] = {
            target: attrs[source] for source, target in (
                ("gen_ai.usage.input_tokens", "input"),
                ("gen_ai.usage.output_tokens", "output"),
            ) if source in attrs
        }
    return body


def _phoenix(span: _Span) -> dict[str, Any]:
    import json

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
        batch = []
        for s in spans:
            body = _langfuse(s)
            if s.parent_id is None:
                root: dict[str, Any] = {k: body[k] for k in ("name", "metadata", "input", "output") if k in body}
                root.update(id=s.trace_id, timestamp=s.start)
                if "gen_ai.conversation.id" in s.attributes:
                    root["sessionId"] = s.attributes["gen_ai.conversation.id"]
                batch.append({"id": secrets.token_hex(16), "timestamp": _now(),
                              "type": "trace-create", "body": root})
            batch.append({"id": secrets.token_hex(16), "timestamp": _now(),
                          "type": "generation-create" if s.operation == "LLM" else "span-create",
                          "body": body})
        return {"batch": batch}

    def _send(self, client: httpx.Client, spans: list[_Span]) -> None:
        # Reuse event IDs on retries: Langfuse deduplicates ingestion events by ID.
        payload = self.payload(spans)
        for attempt in range(3):
            try:
                response = client.post(self.url, headers=self.headers, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.2 * 2 ** attempt)
                        continue
                response.raise_for_status()
                result = response.json()
                if self.provider == "langfuse":
                    accepted = {entry["id"] for entry in result.get("successes", [])
                                if 200 <= entry.get("status", 0) < 300}
                    expected = {entry["id"] for entry in payload["batch"]}
                    if result.get("errors") or accepted != expected:
                        raise ValueError("ingestion rejected events")
                elif result.get("total_queued") != len(spans):
                    raise ValueError("ingestion did not queue all spans")
                return
            except httpx.TransportError:
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


def configure(settings: Any) -> bool:
    """Start one exporter. Environment values override TOML; never read .env files."""
    global _exporter, _record_content
    if _exporter is not None or not getattr(settings, "enabled", False):
        return _exporter is not None
    provider = getattr(settings, "provider", "langfuse")
    headers = {}
    if provider == "langfuse":
        url = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or settings.url or "https://cloud.langfuse.com"
        public = os.getenv("LANGFUSE_PUBLIC_KEY") or settings.public_key
        secret = os.getenv("LANGFUSE_SECRET_KEY") or settings.secret_key
        if not public or not secret:
            logger.warning("Langfuse trace credentials are missing; tracing disabled")
            return False
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()
        path = "/api/public/ingestion"
    elif provider == "phoenix":
        url = os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or settings.url or "http://localhost:6006"
        key = os.getenv("PHOENIX_API_KEY") or settings.api_key
        project = os.getenv("PHOENIX_PROJECT_NAME") or settings.project_name or "default"
        if any(c in project for c in "/?#"):
            logger.warning("Invalid Phoenix trace project name; tracing disabled")
            return False
        if key:
            headers["Authorization"] = f"Bearer {key}"
        path = f"/v1/projects/{quote(project, safe='')}/spans"
    else:
        logger.warning("Unknown trace provider; tracing disabled")
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("invalid base URL")
        if parsed.path.rstrip("/").endswith(("/v1/traces", "/api/public/ingestion")):
            raise ValueError("expected application base URL")
    except ValueError:
        logger.warning("Invalid trace base URL; tracing disabled")
        return False
    _exporter = _Exporter(provider, url.rstrip("/") + path, headers, settings.service_name)
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
                     parent.span_id if parent else None, _record_content)
    recorder.set(**{"service.name": exporter.service})
    if parent and "gen_ai.conversation.id" in parent.attributes:
        recorder.set(**{"gen_ai.conversation.id": parent.attributes["gen_ai.conversation.id"]})
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
