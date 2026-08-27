"""
Anthropic LLM Provider
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import anthropic

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

DEFAULT_MODEL = "claude-opus-5"

# Anthropic's output_config.effort has no "none"/"minimal" rung — "none" instead
# turns thinking off outright (handled separately in stream()), and "minimal"
# has no closer match than "low".
_EFFORT_TO_ANTHROPIC: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


class AnthropicProvider(BaseModelProvider):
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(api_key, base_url)
        self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

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
        """`reasoning_effort="none"` sends `thinking: {"type": "disabled"}`; any
        other value turns on adaptive thinking and sets `output_config.effort`
        (Anthropic's `budget_tokens` token-ceiling style is a 400 on current
        models, so this provider never uses it). Leaving `reasoning_effort`
        unset omits `thinking` entirely, matching current models' own default
        (adaptive thinking already on).

        `temperature`/`top_p` are passed straight through — on Claude Opus 5,
        Sonnet 5, Fable 5, and Opus 4.7/4.8, the API itself returns 400 if
        either is combined with adaptive thinking, i.e. whenever
        `reasoning_effort` is anything but `"none"`.
        """
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._to_tool(tool) for tool in tools]
            if parallel_tool_calls is not None:
                kwargs["tool_choice"] = {
                    "type": "auto",
                    "disable_parallel_tool_use": not parallel_tool_calls,
                }
        if reasoning_effort == "none":
            kwargs["thinking"] = {"type": "disabled"}
        elif reasoning_effort is not None:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": _EFFORT_TO_ANTHROPIC[reasoning_effort]}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if stop is not None:
            kwargs["stop_sequences"] = stop

        # index -> tool_use block id, for pairing content_block_delta/_stop back to a call
        pending_calls: dict[int, str] = {}
        usage = Usage()
        stop_reason: StopReason = "end_turn"

        async with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=[self._to_message(message) for message in messages],
            **kwargs,
        ) as stream:
            async for event in stream:
                if event.type == "message_start":
                    usage.input_tokens = event.message.usage.input_tokens
                elif event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        pending_calls[event.index] = event.content_block.id
                        yield ToolCallStart(id=event.content_block.id, name=event.content_block.name)
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield TextDelta(text=event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        yield ReasoningDelta(text=event.delta.thinking)
                    elif event.delta.type == "input_json_delta":
                        call_id = pending_calls.get(event.index)
                        if call_id is not None:
                            yield ToolCallDelta(id=call_id, partial_json=event.delta.partial_json)
                elif event.type == "content_block_stop":
                    call_id = pending_calls.pop(event.index, None)
                    if call_id is not None:
                        block = event.content_block
                        yield ToolCallEnd(id=call_id, name=block.name, input=block.input)
                elif event.type == "message_delta":
                    usage.output_tokens = event.usage.output_tokens
                    if event.delta.stop_reason is not None:
                        stop_reason = self._map_stop_reason(event.delta.stop_reason)

        yield MessageStop(stop_reason=stop_reason, usage=usage)

    @staticmethod
    def _to_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    @staticmethod
    def _to_message(message: Message) -> dict[str, Any]:
        if isinstance(message.content, str):
            return {"role": message.role, "content": message.content}

        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
            elif isinstance(block, ToolResultBlock):
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        return {"role": message.role, "content": content}

    @staticmethod
    def _map_stop_reason(stop_reason: str) -> StopReason:
        if stop_reason in ("end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"):
            return stop_reason  # type: ignore[return-value]
        return "end_turn"
