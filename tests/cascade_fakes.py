"""Scripted legs for the in-pod cascades (``vamos_voice.providers.cascade``): no vendor, no network.

``ScriptedVad`` hears speech in any frame that is not all zeros (``HELLO`` is
speech, ``SILENCE`` is not). ``FakeSTT`` hands over the turns a test pushes;
``FakeTTS`` turns each word into ``WORD_MS`` of audio; ``FakeLLM`` plays
scripted rounds and records every request. ``with_fake_legs`` puts them into
a real provider instance, so a test drives that vendor's provider through the
shared cascade.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from vamos_voice.providers.base import SessionSetup
from vamos_voice.providers.cascade import (
    CascadeProvider,
    ChatModel,
    Message,
    SpeechToText,
    TextToSpeech,
    ToolCall,
    TurnSink,
)

SILENCE = b"\x00\x00" * 480  # 20 ms
WORD_MS = 100


class ScriptedVad:
    def push(self, frame: bytes) -> bool:
        return any(frame)


class FakeSTT(SpeechToText):
    def __init__(self) -> None:
        self.fed: list[tuple[bytes, bool]] = []
        self.turns: asyncio.Queue[str] = asyncio.Queue()

    async def feed(self, pcm: bytes, speaking: bool) -> None:
        self.fed.append((pcm, speaking))

    async def run(self, on_turn: TurnSink) -> None:
        while True:
            await on_turn(await self.turns.get())


class FakeTTS(TextToSpeech):
    ms_per_word = WORD_MS

    def __init__(self, chunk_delay_s: float = 0.0) -> None:
        self.spoken: list[str] = []
        self.clears = 0
        self.chunk_delay_s = chunk_delay_s

    async def speak(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        whole = "".join([d async for d in text])
        self.spoken.append(whole)
        for _ in whole.split():
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            yield b"\x01\x00" * (24 * WORD_MS)

    async def clear(self) -> None:
        self.clears += 1


class FakeLLM(ChatModel):
    """Plays ``rounds`` in order (each a list of text deltas and ``ToolCall``s); then says "Okay."."""

    def __init__(self, *rounds: list[str | ToolCall]) -> None:
        self.rounds = list(rounds)
        self.requests: list[tuple[list[Message], list[dict[str, Any]]]] = []
        self.requested = asyncio.Event()

    async def reply(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ToolCall]:
        self.requests.append(([dict(m) for m in messages], tools))
        self.requested.set()
        for part in self.rounds.pop(0) if self.rounds else ["Okay."]:
            yield part


def with_fake_legs(
    provider: CascadeProvider, llm: FakeLLM, stt: FakeSTT | None = None, tts: FakeTTS | None = None
) -> tuple[FakeSTT, FakeTTS]:
    """Replace a provider's legs (on the instance) with scripted ones; return the STT and TTS fakes."""
    stt, tts = stt or FakeSTT(), tts or FakeTTS()

    @asynccontextmanager
    async def open_legs(call: Any) -> AsyncIterator[tuple[SpeechToText, TextToSpeech]]:
        yield stt, tts

    def voice_activity(setup: SessionSetup) -> ScriptedVad:
        return ScriptedVad()

    provider.llm = llm
    provider.open_legs = open_legs  # type: ignore[method-assign]
    provider.voice_activity = voice_activity  # type: ignore[method-assign]
    return stt, tts


class ScriptedCascade(CascadeProvider):
    """A cascade with no vendor at all: the shared pipeline under test."""

    name = "scripted"
    required_env = ()

    def __init__(self, llm: FakeLLM, stt: FakeSTT | None = None, tts: FakeTTS | None = None) -> None:
        self.stt, self.tts = with_fake_legs(self, llm, stt, tts)

    @classmethod
    def from_env(cls, env: Any) -> ScriptedCascade:
        raise NotImplementedError

    @asynccontextmanager
    async def open_legs(self, call: Any) -> AsyncIterator[tuple[SpeechToText, TextToSpeech]]:
        yield self.stt, self.tts  # pragma: no cover - replaced on the instance


def tool(name: str, arguments: str = "{}", call_id: str | None = None) -> ToolCall:
    return ToolCall(call_id or f"call_{name}", name, arguments)


async def wait_until(predicate: Any, within: float = 3.0) -> None:
    async def poll() -> None:
        while not predicate():  # noqa: ASYNC110 - polls test state that has no event
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), within)
