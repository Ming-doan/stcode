"""Opt-in real agent + native HTTP ingestion smoke test.

Export provider credentials and STCODE_TRACE_TEST_MODEL, then run:
    uv run pytest -m live tests/integration/trace_live_test.py -s

This test never loads .env. A developer may explicitly inject test-file values into
its process environment. The printed trace IDs identify runs for dashboard review.
"""
import asyncio
import os

import pytest

from stcode.core.agent import Agent, ToolFinished, TurnFinished
from stcode.core.common import trace
from stcode.core.configs import GatewayConfig, TraceConfig
from stcode.core.providers import ProviderConfig, RouteConfig


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
        config = GatewayConfig(trace=TraceConfig(enabled=True, provider=provider, content=True))
        config.providers["openai"] = ProviderConfig(api_key_env="OPENAI_API_KEY", base_url_env="OPENAI_BASE_URL")
        config.routing["high"] = RouteConfig(provider="openai", model=model)
        config.agent.enable_task = False
        config.agent.tools = ["read"]
        config.agent.max_turns = 3
        config.agent.prompt = "Use the read tool to read the requested file, then reply with its contents."
        config.supervisor.enabled = False
        config.session.dir = tmp_path / "sessions"
        config.retry.max_attempts = 1
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
            print(f"\n{provider}: agent.turn {agent.harness.agent_name}; trace_id={next(iter(ids))}; session_id={agent.session.id}")
        finally:
            await agent.aclose()
            await asyncio.to_thread(trace.shutdown)

    asyncio.run(run())


async def _drain(agent, text):
    return [event async for event in agent.run(text)]
