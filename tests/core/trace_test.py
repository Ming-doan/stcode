"""HTTP JSON contracts: Langfuse v4 OTLP, Phoenix REST, no SDK, no implicit credential-file loading."""
import asyncio
import base64
import builtins
import json

import httpx
import pytest

from stcode.core.common import trace
from stcode.core.configs import GatewayConfig, TraceConfig, load_config, redacted


@pytest.fixture(autouse=True)
def isolated_trace(monkeypatch):
    trace.shutdown()
    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY",
                 "LANGFUSE_SECRET_KEY", "PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_API_KEY",
                 "PHOENIX_PROJECT_NAME"):
        monkeypatch.delenv(name, raising=False)
    yield
    trace.shutdown()


@pytest.fixture
def requests(monkeypatch):
    sent = []
    client = httpx.Client

    def handle(request):
        sent.append(request)
        body = json.loads(request.content)
        if "resourceSpans" in body:
            return httpx.Response(200, json={})
        return httpx.Response(202, json={"total_received": len(body["data"]),
                                        "total_queued": len(body["data"])})

    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(handle), **kw))
    return sent


def settings(provider="langfuse", **kw):
    return TraceConfig(enabled=True, provider=provider, url="https://traces.example/prefix",
                       public_key="public", secret_key="secret", **kw)


def spans(requests):
    return [span for request in requests
            for resource in json.loads(request.content)["resourceSpans"]
            for scope in resource["scopeSpans"] for span in scope["spans"]]


def attributes(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_langfuse_v4_has_one_root_and_maps_generations_with_context(requests):
    assert trace.configure(settings())
    with trace.span("agent.turn smoke", attributes={"gen_ai.conversation.id": "session-1",
                                                  "stcode.role": "tester"}):
        tid, root_id = trace.current_ids()
        with trace.span("chat test", kind="client", attributes={"gen_ai.operation.name": "chat",
                                                               "gen_ai.request.model": "test"}) as s:
            s.set(**{"gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 3})
    assert trace.flush()
    assert trace.current_ids() == ("", "")
    assert len(tid) == 32 and len(root_id) == 16
    for request in requests:
        assert str(request.url) == "https://traces.example/prefix/api/public/otel/v1/traces"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"public:secret").decode()
        assert request.headers["x-langfuse-ingestion-version"] == "4"
        assert request.headers["content-type"] == "application/json"
        resource = json.loads(request.content)["resourceSpans"][0]
        assert resource["resource"]["attributes"] == [{"key": "service.name", "value": {"stringValue": "stcode"}}]
        assert resource["scopeSpans"][0]["scope"]["name"] == "stcode"
    records = spans(requests)
    assert len(records) == 2
    root, = [s for s in records if not s.get("parentSpanId")]
    generation, = [s for s in records if s.get("parentSpanId")]
    assert root["spanId"] == root_id
    assert generation["traceId"] == tid and generation["parentSpanId"] == root_id
    assert attributes(root)["langfuse.observation.type"] == "agent"
    assert generation["kind"] == 3
    assert attributes(generation)["langfuse.observation.type"] == "generation"
    assert attributes(generation)["langfuse.observation.model.name"] == "test"
    assert json.loads(attributes(generation)["langfuse.observation.usage_details"]) == {"input": 12, "output": 3}
    for span in records:
        attrs = attributes(span)
        assert attrs["langfuse.trace.name"] == "agent.turn smoke"
        assert attrs["langfuse.session.id"] == "session-1"
        assert attrs["langfuse.trace.metadata.stcode_role"] == "tester"
        assert isinstance(span["startTimeUnixNano"], str)
        assert 0 < int(span["startTimeUnixNano"]) <= int(span["endTimeUnixNano"])


def test_phoenix_uses_native_spans_and_redacts_content_by_default(requests):
    trace.configure(settings("phoenix", api_key="phoenix-key", project_name="my project"))
    with trace.span("chat test", attributes={"gen_ai.operation.name": "chat",
                                            "gen_ai.request.model": "test",
                                            "stcode.input": "private prompt"}) as s:
        s.set(**{"gen_ai.usage.input_tokens": 7, "stcode.output": "private output"})
        s.fail(ValueError("private exception"))
    assert trace.flush()
    request = requests[0]
    assert request.url.raw_path == b"/prefix/v1/projects/my%20project/spans"
    assert request.headers["authorization"] == "Bearer phoenix-key"
    span = json.loads(request.content)["data"][0]
    assert span["span_kind"] == "LLM" and span["status_code"] == "ERROR"
    assert span["attributes"]["llm.model_name"] == "test"
    assert span["attributes"]["llm.token_count.prompt"] == 7
    assert b"private" not in request.content


@pytest.mark.parametrize("provider", ["langfuse", "phoenix"])
def test_content_is_exported_only_when_enabled(provider, requests):
    trace.configure(settings(provider, content=True))
    with trace.span("agent.turn smoke", attributes={"stcode.input": "hello"}) as s:
        s.set(stcode__output="world")
    assert trace.flush()
    body = b"".join(r.content for r in requests)
    assert b"hello" in body and b"world" in body


def test_async_siblings_share_parent_without_leaking_context(requests):
    trace.configure(settings())

    async def run():
        async def child(n):
            with trace.span(f"child {n}"):
                await asyncio.sleep(0)
                return trace.current_ids()
        with trace.span("parent"):
            parent = trace.current_ids()
            children = await asyncio.gather(child(1), child(2))
            assert trace.current_ids() == parent
        assert trace.current_ids() == ("", "")
        return parent, children

    parent, children = asyncio.run(run())
    assert trace.flush()
    assert all(c[0] == parent[0] for c in children)
    assert children[0][1] != children[1][1]
    records = spans(requests)
    assert all(s["parentSpanId"] == parent[1] for s in records if s["name"].startswith("child"))


def test_env_wins_and_tracing_never_imports_otel_or_a_provider_sdk(requests, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *a, **kw):
        assert not name.startswith(("opentelemetry", "langfuse", "phoenix"))
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://env.example")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "env-secret")
    assert trace.configure(settings())
    with trace.span("smoke"):
        pass
    trace.shutdown()
    assert requests[0].url.host == "env.example"
    assert requests[0].headers["authorization"] == "Basic " + base64.b64encode(b"public:env-secret").decode()
    assert not trace.records_content()


def test_missing_langfuse_keys_warns_and_stays_off(caplog):
    assert not trace.configure(TraceConfig(enabled=True, provider="langfuse"))
    assert "credentials" in caplog.text
    assert not trace.enabled()


@pytest.mark.parametrize("status,body,expected_calls", [
    (401, {}, 1), (503, {}, 3), (500, {}, 1),
    (200, {"partialSuccess": {"rejectedSpans": "1", "errorMessage": "secret"}}, 1),
    (202, {"total_queued": 0}, 1),
])
def test_export_failures_do_not_fail_agent_or_leak_responses(monkeypatch, caplog, status, body, expected_calls):
    calls = []
    client = httpx.Client

    def fail(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(fail), **kw))
    trace.configure(settings("phoenix" if status == 202 else "langfuse"))
    with trace.span("agent.turn smoke"):
        pass
    assert not trace.flush()
    assert len(calls) == expected_calls
    assert "export" in caplog.text.lower() and "secret" not in caplog.text


def test_config_does_not_read_dotenv_and_redacts_trace_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LANGFUSE_SECRET_KEY=file-secret\n")
    path = tmp_path / "config.toml"
    path.write_text('[trace]\nprovider="phoenix"\napi_key="config-key"\nsecret_key="secret"\n')
    config = load_config(path)
    import os
    assert "LANGFUSE_SECRET_KEY" not in os.environ
    data = redacted(config)["trace"]
    assert data["api_key"] == "***" and data["secret_key"] == "***"


def test_agent_request_exports_model_and_tool_under_one_turn(requests, tmp_path):
    from fakes import calls_tool, fake_provider, says
    from stcode.core.agent import Agent, TurnFinished
    from stcode.core.providers import ProviderConfig, RouteConfig

    async def run():
        config = GatewayConfig(trace=settings(content=True))
        config.session.dir = tmp_path / "sessions"
        config.providers["fake"] = ProviderConfig()
        config.routing["high"] = RouteConfig(provider="fake", model="fake-small")
        config.supervisor.enabled = False
        config.agent.enable_task = False
        (tmp_path / "hello.txt").write_text("hello tracing")
        with fake_provider([calls_tool("c1", "read", path="hello.txt"), says("hello tracing")]):
            agent = await Agent.create(config, cwd=tmp_path, load_mcp=False)
            try:
                ev = [e async for e in agent.run("Read hello.txt")]
                assert isinstance(ev[-1], TurnFinished)
            finally:
                await agent.aclose()
    asyncio.run(run())
    assert trace.flush()
    observations = spans(requests)
    assert len(observations) == 4
    root, = [s for s in observations if not s.get("parentSpanId")]
    assert attributes(root)["langfuse.observation.input"] == "Read hello.txt"
    assert attributes(root)["langfuse.observation.output"] == "hello tracing"
    assert all(s["status"]["code"] == 1 for s in observations)
    assert len([s for s in observations if attributes(s)["langfuse.observation.type"] == "generation"]) == 2
    tool, = [s for s in observations if s["name"] == "execute_tool read"]
    assert attributes(tool)["langfuse.observation.type"] == "tool"
    assert {s["traceId"] for s in observations} == {root["traceId"]}
    assert all(s["parentSpanId"] == root["spanId"] for s in observations if s is not root)


def test_http_export_never_blocks_span_completion_and_shutdown_drains(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    sent = []
    client = httpx.Client

    def slow(request):
        entered.set()
        assert release.wait(5)
        sent.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(slow), **kw))
    trace.configure(settings())
    try:
        with trace.span("first"):
            pass
        assert entered.wait(5)
        with trace.span("second"):
            pass  # Completes while the worker is blocked on the first HTTP request.
        assert not trace.flush(timeout=0.01)
    finally:
        release.set()
        trace.shutdown()
    assert len(spans(sent)) == 2
    assert not trace.enabled()


def test_connection_failure_is_retried_with_same_completed_spans(monkeypatch):
    sent = []
    client = httpx.Client

    def handle(request):
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ConnectTimeout("secret endpoint", request=request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    trace.configure(settings())
    with trace.span("smoke"):
        pass
    assert trace.flush()
    assert len(sent) == 2 and sent[0].content == sent[1].content


def test_exception_is_exported_and_original_error_propagates(requests):
    trace.configure(settings())
    with pytest.raises(ValueError, match="private"):
        with trace.span("failing"):
            raise ValueError("private")
    assert trace.current_ids() == ("", "")
    assert trace.flush()
    observation, = spans(requests)
    assert observation["status"] == {"code": 2, "message": "ValueError"}


def test_phoenix_environment_selects_url_key_and_project(requests, monkeypatch):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "https://phoenix.example/proxy/")
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "environment")
    monkeypatch.setenv("PHOENIX_API_KEY", "env-key")
    trace.configure(settings("phoenix", api_key="config-key", project_name="config-project"))
    with trace.span("smoke"):
        pass
    assert trace.flush()
    assert str(requests[0].url) == "https://phoenix.example/proxy/v1/projects/environment/spans"
    assert requests[0].headers["authorization"] == "Bearer env-key"


def test_shutdown_allows_reconfiguration_without_previous_content_setting(requests):
    trace.configure(settings(content=True))
    trace.shutdown()
    assert not trace.records_content()
    trace.configure(settings("phoenix"))
    with trace.span("second"):
        pass
    assert trace.flush()
    assert "data" in json.loads(requests[0].content)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError])
def test_langfuse_ambiguous_delivery_is_not_replayed(monkeypatch, failure):
    sent = []
    client = httpx.Client

    def handle(request):
        sent.append(request)
        raise failure("secret endpoint", request=request)

    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    trace.configure(settings())
    with trace.span("smoke"):
        pass
    assert not trace.flush()
    assert len(sent) == 1


def test_otlp_values_preserve_types_and_timestamp_precision(requests):
    from datetime import datetime, timezone
    trace.configure(settings())
    with trace.span("smoke", attributes={"count": 123, "ratio": 0.25, "ok": True,
                                        "labels": ["a", "b"], "object": {"x": 1},
                                        "stcode.input": "private"}):
        pass
    assert trace.flush()
    record, = spans(requests)
    values = {a["key"]: a["value"] for a in record["attributes"]}
    assert values["count"] == {"intValue": "123"}
    assert values["ratio"] == {"doubleValue": 0.25}
    assert values["ok"] == {"boolValue": True}
    assert values["labels"] == {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}}
    assert json.loads(values["object"]["stringValue"]) == {"x": 1}
    assert "private" not in json.dumps(record)
    assert datetime.now(timezone.utc).year == datetime.fromtimestamp(int(record["startTimeUnixNano"]) / 1e9, timezone.utc).year


@pytest.mark.parametrize("body", [{}, {"partialSuccess": {}}, {"partialSuccess": {"rejectedSpans": "0"}}])
def test_otlp_full_success_is_accepted(monkeypatch, body):
    client = httpx.Client
    monkeypatch.setattr(trace.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)), **kw))
    trace.configure(settings())
    with trace.span("smoke"):
        pass
    assert trace.flush()
