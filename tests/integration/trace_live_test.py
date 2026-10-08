"""Opt-in real agent + direct HTTP ingestion smoke test.

Export provider credentials and STCODE_TRACE_TEST_MODEL, then run:
    uv run pytest -m live tests/integration/trace_live_test.py -s

This test never loads .env. A developer may explicitly inject test-file values into
its process environment. The printed trace IDs identify runs for dashboard review.
"""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

import pytest

from stcode.core.agent import Agent, ToolFinished, TurnFinished
from stcode.core.common import trace
from stcode.core.configs import Config, TraceConfig


@pytest.mark.live
@pytest.mark.parametrize("provider", ["langfuse", "phoenix"])
def test_real_agent_logs_a_read_tool_and_reply(provider, tmp_path):
    model = os.getenv("STCODE_TRACE_TEST_MODEL")
    if not model or not os.getenv("OPENAI_API_KEY"):
        pytest.skip("Set STCODE_TRACE_TEST_MODEL and OPENAI_API_KEY for the live agent")
    required = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if provider == "langfuse" else ("PHOENIX_COLLECTOR_ENDPOINT",)
    if any(not os.getenv(k) for k in required):
        pytest.skip(f"Set {', '.join(required)} for {provider}")

    async def run():
        trace.shutdown()
        config = Config.model_validate({"model": {"providers": {"openai": {
            "provider": "openai", "model": model, "base_url": "${OPENAI_BASE_URL}"}}}})
        config.trace = TraceConfig(enabled=True, provider=provider, content=True)
        config.agent.enable_task = False
        config.agent.tools = ["read"]
        config.agent.max_turns = 3
        config.agent.prompt = "Use the read tool to read the requested file, then reply with its contents."
        config.supervisor.enabled = False
        config.session.dir = tmp_path / "sessions"
        config.model.retry.max_attempts = 1
        (tmp_path / "trace-smoke.txt").write_text("STCODE_HTTP_TRACE_OK\n")
        agent = await Agent.create(config, cwd=tmp_path, load_mcp=False)
        agent.harness.agent_name = f"stcode-http-acceptance-{provider}"
        try:
            events = await asyncio.wait_for(
                _drain(agent, "Read trace-smoke.txt using the read tool and report its exact contents."),
                timeout=120,
            )
            assert isinstance(events[-1], TurnFinished), events[-1]
            assert "STCODE_HTTP_TRACE_OK" in events[-1].text
            assert any(isinstance(e, ToolFinished) and e.name == "read" and e.ok for e in events)
            ids = {r["trace_id"] for r in agent.session.records() if r.get("type") == "usage" and r.get("trace_id")}
            assert len(ids) == 1
            assert await asyncio.to_thread(trace.flush), "Trace ingestion failed"
            trace_id = next(iter(ids))
            print(f"\n{provider}: agent.turn {agent.harness.agent_name}; trace_id={trace_id}; session_id={agent.session.id}")
            if provider == "langfuse":
                expected = 1 + sum(r.get("type") == "usage" for r in agent.session.records())
                expected += sum(isinstance(e, ToolFinished) for e in events)
                await _verify_langfuse(trace_id, agent.session.id, model, expected)
        finally:
            await agent.aclose()
            await asyncio.to_thread(trace.shutdown)

    asyncio.run(run())


async def _drain(agent, text):
    return [event async for event in agent.run(text)]


async def _verify_langfuse(trace_id, session_id, model, expected):
    """Verify persisted v4 observations rather than treating HTTP acceptance as visibility."""
    base = (os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
    now = datetime.now(timezone.utc)
    started = time.monotonic()
    rows = []
    async with httpx.AsyncClient(
        auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]), timeout=15,
    ) as client:
        for attempt in range(30):
            response = await client.get(base + "/api/public/v2/observations", params={
                "traceId": trace_id, "fields": "core,basic,usage,model,io,trace_context", "limit": 100,
                "fromStartTime": (now - timedelta(minutes=10)).isoformat(),
                "toStartTime": (now + timedelta(minutes=2)).isoformat(),
            })
            assert response.status_code == 200, f"Readback HTTP {response.status_code}"
            rows = response.json()["data"]
            if len(rows) >= expected:
                break
            if attempt < 29:
                await asyncio.sleep(2)
    assert len(rows) == expected, f"Expected {expected} spans, read back {len(rows)} for {trace_id}"
    assert len({row["id"] for row in rows}) == expected, "Duplicate observation IDs"
    root, = [row for row in rows if not row.get("parentObservationId")]
    assert root["type"] == "AGENT" and root["isRootObservation"]
    assert sum(bool(row.get("isRootObservation")) for row in rows) == 1
    assert "Read trace-smoke.txt" in root["input"]
    assert "STCODE_HTTP_TRACE_OK" in root["output"]
    assert all(row["traceId"] == trace_id and row["sessionId"] == session_id for row in rows)
    assert all(row["traceName"] == root["name"] for row in rows)
    assert all(row.get("level") != "ERROR" for row in rows)
    assert all(row["parentObservationId"] == root["id"] for row in rows if row is not root)
    assert any(row["type"] == "TOOL" for row in rows)
    generations = [row for row in rows if row["type"] == "GENERATION"]
    assert len(generations) >= 2
    for row in generations:
        assert (row.get("providedModelName") or row.get("model")) == model
        assert row["usageDetails"]["input"] > 0 and row["usageDetails"]["output"] > 0
    print(f"Langfuse v4 readback verified: {len(rows)} observations, one root; visible after {time.monotonic() - started:.1f}s")
