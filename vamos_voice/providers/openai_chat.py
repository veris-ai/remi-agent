"""The LLM leg of a cascade on an OpenAI-compatible chat-completions API, through the ``openai`` SDK.

The Hugging Face router, xAI and OpenAI all serve the chat-completions API,
and the bench's OTel bundle instruments the ``openai`` SDK (OpenInference
``OpenAIInstrumentor``), so every round lands in the agent trace as an LLM span.
Rounds stream: text deltas go to the TTS leg as they arrive, and tool calls,
which stream as fragments keyed by index, are assembled and yielded at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from openai import AsyncOpenAI

from .base import OPENAI_SDK_MAX_RETRIES
from .cascade import ChatModel, Message, ToolCall

#: Every LLM request's ceiling: a hung provider must not hold the call forever.
TIMEOUT_S = 60.0


def openai_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """An SDK client with the cascade's timeout and the shared retry budget (429, 5xx, connection errors)."""
    return AsyncOpenAI(
        base_url=base_url, api_key=api_key, timeout=TIMEOUT_S, max_retries=OPENAI_SDK_MAX_RETRIES
    )


class OpenAIChat(ChatModel):
    """Streamed chat completions on one model; ``extra`` goes into every request (e.g. ``reasoning_effort``)."""

    def __init__(self, client: AsyncOpenAI, model: str, extra: Mapping[str, Any] | None = None) -> None:
        self.client, self.model, self.extra = client, model, dict(extra or {})

    async def reply(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ToolCall]:
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice="auto",
            stream=True,
            **self.extra,
        )
        parts: dict[int, dict[str, str]] = {}
        async with stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                for fragment in delta.tool_calls or []:
                    part = parts.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                    part["id"] = fragment.id or part["id"]
                    if fragment.function is not None:
                        part["name"] += fragment.function.name or ""
                        part["arguments"] += fragment.function.arguments or ""
                if delta.content:
                    yield delta.content
        for index in sorted(parts):
            part = parts[index]
            yield ToolCall(part["id"] or f"call_{index}", part["name"], part["arguments"])
