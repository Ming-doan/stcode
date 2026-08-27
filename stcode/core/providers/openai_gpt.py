"""
OpenAI LLM Provider
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI

from stcode.core.providers.base import BaseModelProvider
from stcode.core.providers.types import (
    Message,
    MessageStop,
    ReasoningDelta,
    ReasoningEffort,
    StopReason,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

DEFAULT_MODEL = "gpt-5.6"

_FINISH_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class OpenAIProvider(BaseModelProvider):
    """Talks to the OpenAI Chat Completions protocol — the de facto standard for
    third-party OpenAI-compatible routers/proxies (unlike the newer Responses API,
    which is rarely implemented outside api.openai.com itself).
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(api_key, base_url)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def list_models(self) -> list[str]:
        return [model.id async for model in self._client.models.list()]

    async def aclose(self) -> None:
        await self._client.close()

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str = DEFAULT_MODEL,
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 8192,
        reasoning_effort: ReasoningEffort | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """`reasoning_effort` maps 1:1 onto Chat Completions' own `reasoning_effort`
        field — OpenAI already uses this exact seven-value scale. Support for
        each value is model-dependent; the API rejects a value the target model
        doesn't recognize rather than us pre-validating it here.
        """
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [self._to_tool(tool) for tool in tools]
            if parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = parallel_tool_calls
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if stop is not None:
            kwargs["stop"] = stop

        # index -> accumulated call state, for pairing deltas back to a call
        pending_calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        stop_reason: StopReason = "end_turn"

        response_stream = await self._client.chat.completions.create(
            model=model,
            messages=self._to_messages(messages, system),
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        async for chunk in response_stream:
            if chunk.usage is not None:
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                )
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                yield TextDelta(text=delta.content)

            # Not a Chat Completions field — some OpenAI-compatible routers (e.g.
            # reasoning-model proxies) bolt it onto the delta as an extra attribute.
            reasoning = getattr(delta, "reasoning", None)
            if reasoning:
                yield ReasoningDelta(text=reasoning)

            for tool_call_delta in delta.tool_calls or []:
                index = tool_call_delta.index
                if tool_call_delta.id is not None:
                    # First chunk for this call: id + name arrive together.
                    name = tool_call_delta.function.name if tool_call_delta.function else ""
                    pending_calls[index] = {"id": tool_call_delta.id, "name": name, "json": ""}
                    yield ToolCallStart(id=tool_call_delta.id, name=name)
                call = pending_calls.get(index)
                if call is None:
                    continue
                arguments = tool_call_delta.function.arguments if tool_call_delta.function else None
                if arguments:
                    call["json"] += arguments
                    yield ToolCallDelta(id=call["id"], partial_json=arguments)

            if choice.finish_reason is not None:
                stop_reason = _FINISH_REASON_MAP.get(choice.finish_reason, "end_turn")
                # Some OpenAI-compatible routers repeat finish_reason on a trailing
                # usage-only chunk — clear so that chunk doesn't re-flush stale calls.
                for call in pending_calls.values():
                    yield ToolCallEnd(
                        id=call["id"],
                        name=call["name"],
                        input=json.loads(call["json"]) if call["json"] else {},
                    )
                pending_calls.clear()

        yield MessageStop(stop_reason=stop_reason, usage=usage)

    @staticmethod
    def _to_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _to_messages(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
        chat_messages: list[dict[str, Any]] = []
        if system:
            chat_messages.append({"role": "system", "content": system})

        for message in messages:
            if isinstance(message.content, str):
                chat_messages.append({"role": message.role, "content": message.content})
                continue

            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {"name": block.name, "arguments": json.dumps(block.input)},
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    # Chat Completions has no first-class error flag on tool messages —
                    # fold it into the content so the model still sees the failure.
                    content = f"Error: {block.content}" if block.is_error else block.content
                    chat_messages.append(
                        {"role": "tool", "tool_call_id": block.tool_use_id, "content": content}
                    )

            if text_parts or tool_calls:
                turn: dict[str, Any] = {"role": message.role, "content": "".join(text_parts) or None}
                if tool_calls:
                    turn["tool_calls"] = tool_calls
                chat_messages.append(turn)

        return chat_messages
