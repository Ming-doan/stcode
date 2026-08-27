"""
Google GenAI (Gemini) LLM Provider
"""

from __future__ import annotations

import json
from typing import AsyncGenerator

from google import genai
from google.genai import types

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

DEFAULT_MODEL = "gemini-3.7-flash"

# ThinkingConfig.thinking_level tops out at HIGH — there's no xhigh/max rung,
# so both clamp down to it. "none" is handled separately via thinking_budget=0.
_EFFORT_TO_GEMINI_THINKING_LEVEL: dict[str, str] = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}

_ROLE_MAP = {"user": "user", "assistant": "model"}

_REFUSAL_FINISH_REASONS = {
    types.FinishReason.SAFETY,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.SPII,
    types.FinishReason.RECITATION,
    types.FinishReason.IMAGE_SAFETY,
    types.FinishReason.IMAGE_PROHIBITED_CONTENT,
}


class GoogleGenAIProvider(BaseModelProvider):
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        super().__init__(api_key, base_url)
        http_options = types.HttpOptions(base_url=base_url) if base_url else None
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    async def list_models(self) -> list[str]:
        return [model.name.removeprefix("models/") async for model in self._client.aio.models.list()]

    async def aclose(self) -> None:
        await self._client.aio.aclose()

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
        """`reasoning_effort="none"` sets `thinking_budget=0` (thinking off);
        any other value maps onto `thinking_config.thinking_level`, clamped to
        Gemini's four-level scale (`xhigh`/`max` both become `HIGH`). Thoughts
        are always requested (`include_thoughts=True`) so a thinking-capable
        model's reasoning surfaces as `ReasoningDelta` regardless of level —
        it's a no-op on models that don't support thinking.

        `parallel_tool_calls` has no Gemini equivalent exposed by this SDK and
        is accepted but ignored.
        """
        thinking_config = (
            types.ThinkingConfig(thinking_budget=0)
            if reasoning_effort == "none"
            else types.ThinkingConfig(
                include_thoughts=True,
                thinking_level=(
                    _EFFORT_TO_GEMINI_THINKING_LEVEL[reasoning_effort] if reasoning_effort else None
                ),
            )
        )
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            tools=[self._to_tool(tools)] if tools else None,
            thinking_config=thinking_config,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop,
        )

        has_tool_call = False
        finish_reason: types.FinishReason | None = None
        usage = Usage()

        response_stream = await self._client.aio.models.generate_content_stream(
            model=model,
            contents=self._to_contents(messages),
            config=config,
        )
        async for chunk in response_stream:
            for candidate in chunk.candidates or []:
                if candidate.finish_reason is not None:
                    finish_reason = candidate.finish_reason
                if candidate.content is None or not candidate.content.parts:
                    continue
                for part in candidate.content.parts:
                    if part.text:
                        if part.thought:
                            yield ReasoningDelta(text=part.text)
                        else:
                            yield TextDelta(text=part.text)
                    elif part.function_call is not None:
                        has_tool_call = True
                        call = part.function_call
                        call_id = call.id or call.name
                        args = call.args or {}
                        yield ToolCallStart(id=call_id, name=call.name)
                        yield ToolCallDelta(id=call_id, partial_json=json.dumps(args))
                        yield ToolCallEnd(id=call_id, name=call.name, input=args)
            if chunk.usage_metadata is not None:
                usage = Usage(
                    input_tokens=chunk.usage_metadata.prompt_token_count or 0,
                    output_tokens=chunk.usage_metadata.candidates_token_count or 0,
                )

        yield MessageStop(
            stop_reason=self._map_stop_reason(has_tool_call, finish_reason),
            usage=usage,
        )

    @staticmethod
    def _to_tool(tools: list[ToolDefinition]) -> types.Tool:
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema,
                )
                for tool in tools
            ]
        )

    @staticmethod
    def _to_contents(messages: list[Message]) -> list[types.Content]:
        # Gemini matches tool results by name, not id — recover each id's tool name
        # from the tool_use blocks that precede it in the conversation.
        tool_names: dict[str, str] = {}
        for message in messages:
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_names[block.id] = block.name

        contents: list[types.Content] = []
        for message in messages:
            blocks = (
                [TextBlock(text=message.content)]
                if isinstance(message.content, str)
                else message.content
            )
            buffer: list[types.Part] = []
            buffer_role: str | None = None
            for block in blocks:
                if isinstance(block, TextBlock):
                    role = _ROLE_MAP[message.role]
                    part = types.Part.from_text(text=block.text)
                elif isinstance(block, ToolUseBlock):
                    role = "model"
                    part = types.Part(
                        function_call=types.FunctionCall(id=block.id, name=block.name, args=block.input)
                    )
                elif isinstance(block, ToolResultBlock):
                    role = "tool"
                    name = tool_names.get(block.tool_use_id, block.tool_use_id)
                    response = {"error": block.content} if block.is_error else {"result": block.content}
                    part = types.Part(
                        function_response=types.FunctionResponse(
                            id=block.tool_use_id, name=name, response=response
                        )
                    )
                else:
                    continue

                if buffer_role is not None and buffer_role != role:
                    contents.append(types.Content(role=buffer_role, parts=buffer))
                    buffer = []
                buffer_role = role
                buffer.append(part)
            if buffer:
                contents.append(types.Content(role=buffer_role, parts=buffer))
        return contents

    @staticmethod
    def _map_stop_reason(has_tool_call: bool, finish_reason: types.FinishReason | None) -> StopReason:
        if has_tool_call:
            return "tool_use"
        if finish_reason == types.FinishReason.MAX_TOKENS:
            return "max_tokens"
        if finish_reason in _REFUSAL_FINISH_REASONS:
            return "refusal"
        return "end_turn"
