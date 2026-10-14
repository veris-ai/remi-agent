"""Pipecat's OpenAI TTS against a local speech server that stalls or fails before it answers.

OpenAI's speech endpoint sometimes holds a request for 25 s or more before its first byte;
the same request retried answers at once. ``RetryingOpenAITTS`` must give up on the stalled
attempt and retry (a 5xx too), so the reply is spoken instead of dropped.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed"
)

AUDIO = b"\x01\x00" * 2400  # 100 ms of PCM16 at 24 kHz

Behaviour = Callable[[int], str]  # attempt number (1-based) -> "stall" | "500" | "ok"


@asynccontextmanager
async def speech_server(behaviour: Behaviour) -> AsyncIterator[tuple[str, list[int]]]:
    """A local ``/v1/audio/speech``: each request does what ``behaviour`` says for its attempt."""
    attempts: list[int] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":")[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length")
        )
        await reader.readexactly(length)
        attempts.append(len(attempts) + 1)
        action = behaviour(len(attempts))
        if action == "stall":
            await asyncio.sleep(30)  # never answers in time
        elif action == "500":
            body = b'{"error": {"message": "overloaded"}}'
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n")
            writer.write(b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n")
            writer.write(b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(AUDIO) + AUDIO)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1", attempts
    finally:
        server.close()


async def synthesize(base_url: str, monkeypatch: pytest.MonkeyPatch) -> bytes:
    from pipecat.frames.frames import TTSAudioRawFrame

    from vamos_voice.providers.pipecat import services

    monkeypatch.setattr(services, "TTS_ATTEMPT_TIMEOUT_S", 0.3)
    tts = services.RetryingOpenAITTS(
        api_key="test",
        base_url=base_url,
        sample_rate=24000,
        settings=services.OpenAITTSService.Settings(model="gpt-4o-mini-tts", voice="marin"),
    )
    tts._sample_rate = 24000  # set by the pipeline's StartFrame in a call
    audio = b""
    async for frame in tts.run_tts("Hi, this is Remi.", "ctx-1"):
        assert not hasattr(frame, "error"), frame
        if isinstance(frame, TTSAudioRawFrame):
            audio += frame.audio
    return audio


async def test_a_stalled_speech_request_is_retried_and_the_reply_is_spoken(monkeypatch) -> None:
    async with speech_server(lambda n: "stall" if n <= 2 else "ok") as (url, attempts):
        audio = await asyncio.wait_for(synthesize(url, monkeypatch), 10)
    assert audio == AUDIO
    assert attempts == [1, 2, 3]


async def test_a_5xx_is_retried(monkeypatch) -> None:
    async with speech_server(lambda n: "500" if n == 1 else "ok") as (url, attempts):
        audio = await asyncio.wait_for(synthesize(url, monkeypatch), 10)
    assert audio == AUDIO and attempts == [1, 2]


def test_the_retries_fit_the_shared_budget_and_pipecat_waits_for_them() -> None:
    from openai._constants import INITIAL_RETRY_DELAY, MAX_RETRY_DELAY

    from vamos_voice.providers.base import RETRY_BUDGET_S
    from vamos_voice.providers.pipecat import services
    from vamos_voice.providers.pipecat.services import TTS_FACTORIES, ServiceConfig

    backoff = sum(min(INITIAL_RETRY_DELAY * 2**k, MAX_RETRY_DELAY) for k in range(services.TTS_MAX_RETRIES))
    worst = (services.TTS_MAX_RETRIES + 1) * services.TTS_ATTEMPT_TIMEOUT_S + backoff
    assert worst <= RETRY_BUDGET_S < services.TTS_IDLE_TIMEOUT_S

    tts = TTS_FACTORIES["openai"](ServiceConfig(openai_api_key="k", stt="openai", tts="openai"))
    assert isinstance(tts, services.RetryingOpenAITTS)
    assert tts._client.max_retries == services.TTS_MAX_RETRIES
    assert tts._client.timeout == services.TTS_ATTEMPT_TIMEOUT_S
    assert tts._stop_frame_timeout_s == services.TTS_IDLE_TIMEOUT_S
    assert (tts._settings.model, tts._settings.voice) == ("gpt-4o-mini-tts", "marin"), (
        "voice and model unchanged"
    )
