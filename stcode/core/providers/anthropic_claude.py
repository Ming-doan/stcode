"""
Anthropic LLM provider.

**Prompt caching lives here and nowhere else.** It is the single biggest cost lever, and
the one optimisation that has to be applied at the provider boundary: only Anthropic
asks for it explicitly. OpenAI caches long prefixes on its own, and Gemini needs a
separate cached-content resource, which is a different feature. See the `prompt caching`
section at the bottom for where the three markers go and why.
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
            kwargs["system"] = _mark_last([{"type": "text", "text": system}])
        if tools:
            kwargs["tools"] = _mark_last([self._to_tool(tool) for tool in tools])
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
            messages=_mark_last_block([self._to_message(message) for message in messages]),
            **kwargs,
        ) as stream:
            async for event in stream:
                if event.type == "message_start":
                    started = event.message.usage
                    usage.input_tokens = started.input_tokens
                    # Present only when caching is in play, and the whole point of it:
                    # a non-zero read on turn 2 is the evidence the prefix held.
                    usage.cache_creation_input_tokens = (
                        getattr(started, "cache_creation_input_tokens", None) or 0
                    )
                    usage.cache_read_input_tokens = (
                        getattr(started, "cache_read_input_tokens", None) or 0
                    )
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


# ---- prompt caching --------------------------------------------------------------
#
# Anthropic caches the request *prefix*, in the order tools -> system -> messages, up
# to each breakpoint. Three breakpoints, and each earns its place:
#
#   last tool     `registry.definitions()` sorts by name, so the tool block is
#                 byte-stable for a session and survives a turn where the system
#                 prompt moved underneath it.
#   last system   Stable too, except on turns where the todo list changed —
#                 `environment_section` is deliberately last for exactly this reason.
#   last message  The one that actually matters in an agent loop. After ten tool calls
#                 the history dwarfs both of the above, so without a breakpoint here
#                 "turn 2 is ~10x cheaper" would be a rounding error rather than a
#                 fact. Each turn's breakpoint is the next turn's cache hit.
#
# A write costs 1.25x and a read 0.1x, so this pays for itself on the second request
# and every one after. A single-shot call pays the premium for nothing — the deliberate
# trade, because this project is an agent loop.

CACHE_CONTROL = {"type": "ephemeral"}


def _mark_last(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put a cache breakpoint on the final entry of `blocks`."""
    if blocks:
        blocks[-1] = {**blocks[-1], "cache_control": CACHE_CONTROL}
    return blocks


def _mark_last_block(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put a cache breakpoint on the last content block of the last message.

    A string `content` is promoted to a one-block list first: `cache_control` is a
    property of a block, and there is nowhere to hang it on a bare string.
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not content:
        return messages
    last["content"] = [*content[:-1], {**content[-1], "cache_control": CACHE_CONTROL}]
    messages[-1] = last
    return messages
