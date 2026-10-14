"""The agent on Mistral: Voxtral Realtime STT → a Mistral chat model → Voxtral TTS, cascaded in-pod.

Mistral sells the three legs separately, so the pipeline is the shared
in-pod cascade (``vamos_voice.providers.cascade``); this module is the legs,
as on the VAmoS Energy Mistral candidate (rory-agent ``mistral/``):

- **STT**: Voxtral Realtime over a websocket (``wire.connect_websocket``):
  ``input_audio.append`` of 16 kHz PCM16 (the caller's 24 kHz resampled; 16 kHz
  is the rate Mistral documents). Voxtral Realtime documents no turn detection,
  so the end of a turn is local: ``TurnTaking.silence_ms`` of non-speech
  after Silero heard speech (``SilenceEndpoint``), then ``input_audio.flush``,
  whose ``transcription.done`` carries the turn's text (only the audio since
  the previous flush).
- **LLM**: ``chat.complete_async`` on the ``mistralai`` SDK (traced by the
  bench's ``MistralAIInstrumentor``), with the SDK's backoff on: a turn spends
  two or three completions on tools, enough to hit Mistral's rate limit, and a
  throttled request without retries would end the call.
- **TTS**: ``audio.speech.complete_async(stream=True, response_format="pcm")``:
  float32 at 24 kHz, the caller's rate, converted to PCM16. The voice is a
  preset slug resolved to its id at startup, so an unknown voice fails the boot.

Config (AGENTS.md section 11): ``MISTRAL_API_KEY`` (required);
``VAMOS_MISTRAL_LLM_MODEL`` / ``VAMOS_MISTRAL_STT_MODEL`` /
``VAMOS_MISTRAL_TTS_MODEL`` / ``VAMOS_MISTRAL_VOICE``, defaulting to the
Energy candidate's ``mistral-medium-2604`` / ``voxtral-mini-transcribe-realtime-2602``
/ ``voxtral-mini-tts-2603`` / ``en_paul_neutral``.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlencode

import numpy as np
import websockets
from mistralai.client import Mistral
from mistralai.client.utils import BackoffStrategy, RetryConfig

from vamos_voice.audio import CHANNEL_RATE_HZ, Resampler
from vamos_voice.providers.base import RETRY_BUDGET_S, CallRuntime
from vamos_voice.providers.cascade import (
    CascadeProvider,
    ChatModel,
    Message,
    SilenceEndpoint,
    SpeechToText,
    TextToSpeech,
    ToolCall,
    TurnSink,
)

logger = logging.getLogger(__name__)

STT_URL = "wss://api.mistral.ai/v1/audio/transcriptions/realtime"
STT_RATE_HZ = 16_000
#: Every Mistral HTTP request's ceiling, retries included (throttling arrives in bursts of seconds).
REQUEST_TIMEOUT_MS = 30_000
RETRIES = RetryConfig(
    "backoff",
    BackoffStrategy(
        initial_interval=200,
        max_interval=4_000,
        exponent=1.5,
        max_elapsed_time=int(RETRY_BUDGET_S * 1000),
    ),
    retry_connection_errors=True,
)


@dataclass(frozen=True)
class MistralConfig:
    api_key: str = field(repr=False)
    llm_model: str = "mistral-medium-2604"
    stt_model: str = "voxtral-mini-transcribe-realtime-2602"
    tts_model: str = "voxtral-mini-tts-2603"
    voice: str = "en_paul_neutral"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MistralConfig:
        return cls(
            api_key=env["MISTRAL_API_KEY"],
            llm_model=env.get("VAMOS_MISTRAL_LLM_MODEL") or cls.llm_model,
            stt_model=env.get("VAMOS_MISTRAL_STT_MODEL") or cls.stt_model,
            tts_model=env.get("VAMOS_MISTRAL_TTS_MODEL") or cls.tts_model,
            voice=env.get("VAMOS_MISTRAL_VOICE") or cls.voice,
        )


class MistralChat(ChatModel):
    """One non-streamed completion per round (Voxtral TTS takes the whole text anyway)."""

    def __init__(self, client: Mistral, model: str) -> None:
        self.client, self.model = client, model

    async def reply(
        self, messages: list[Message], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str | ToolCall]:
        response = await self.client.chat.complete_async(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",  # type: ignore[arg-type]
        )
        message = response.choices[0].message
        if isinstance(message.content, str) and message.content:
            yield message.content
        for i, call in enumerate(message.tool_calls or []):
            arguments = call.function.arguments
            yield ToolCall(
                call.id or f"call_{i}",
                call.function.name,
                arguments if isinstance(arguments, str) else json.dumps(arguments),
            )


class VoxtralRealtime(SpeechToText):
    """Voxtral Realtime transcription, with the turn ended locally by a flush after silence."""

    def __init__(self, ws: Any, silence_ms: int) -> None:
        self.ws = ws
        self.endpoint = SilenceEndpoint(silence_ms)
        self.resampler = Resampler(CHANNEL_RATE_HZ, STT_RATE_HZ)

    async def start(self) -> None:
        """Wait for ``session.created``, then declare the audio format."""
        created = json.loads(await self.ws.recv())
        if created.get("type") != "session.created":
            raise RuntimeError(
                f"Voxtral Realtime opened with {created.get('type')!r}: {created.get('error')}"
            )
        audio_format = {"encoding": "pcm_s16le", "sample_rate": STT_RATE_HZ}
        await self.ws.send(json.dumps({"type": "session.update", "session": {"audio_format": audio_format}}))

    async def feed(self, pcm: bytes, speaking: bool) -> None:
        audio = base64.b64encode(self.resampler.process(pcm)).decode()
        await self.ws.send(json.dumps({"type": "input_audio.append", "audio": audio}))
        if self.endpoint.push(len(pcm), speaking):
            await self.ws.send(json.dumps({"type": "input_audio.flush"}))

    async def run(self, on_turn: TurnSink) -> None:
        while True:
            try:
                event = json.loads(await self.ws.recv())
            except websockets.ConnectionClosed as closed:
                logger.info("Voxtral Realtime closed: %s", closed)
                return
            kind = event.get("type")
            if kind == "transcription.done":
                await on_turn(event.get("text") or "")
            elif kind == "error":
                raise RuntimeError(f"Voxtral Realtime error: {event.get('error')}")


class VoxtralTTS(TextToSpeech):
    """Voxtral TTS, streamed: float32 24 kHz chunks as they are synthesized."""

    ms_per_word = 400  # the Energy candidate's figure for the en_paul voices

    def __init__(self, client: Mistral, model: str, voice_id: str) -> None:
        self.client, self.model, self.voice_id = client, model, voice_id

    async def speak(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        whole = "".join([delta async for delta in text]).strip()
        if not whole:
            return
        stream = await self.client.audio.speech.complete_async(
            model=self.model, input=whole, voice_id=self.voice_id, response_format="pcm", stream=True
        )
        async for event in stream:
            if getattr(event.data, "type", None) == "speech.audio.delta":
                yield float32_to_pcm16(base64.b64decode(event.data.audio_data))


def float32_to_pcm16(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype="<f4")
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class MistralProvider(CascadeProvider):
    name = "mistral"
    required_env = ("MISTRAL_API_KEY",)

    def __init__(self, config: MistralConfig, client: Mistral | None = None) -> None:
        self.config = config
        self.client = client or Mistral(
            api_key=config.api_key, retry_config=RETRIES, timeout_ms=REQUEST_TIMEOUT_MS
        )
        self.llm = MistralChat(self.client, config.llm_model)
        self.voice_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(MistralConfig.from_env(env))

    async def warm_up(self) -> None:
        await super().warm_up()
        await self.resolve_voice()

    async def resolve_voice(self) -> str:
        """The preset's id (``voice_id`` takes the id, not the slug); an unknown slug fails the boot."""
        voices = await self.client.audio.voices.list_async(type_="preset", limit=100)
        matches = [v.id for v in voices.items if v.slug == self.config.voice]
        if not matches:
            known = ", ".join(sorted(v.slug for v in voices.items if v.slug))
            raise RuntimeError(f"VAMOS_MISTRAL_VOICE {self.config.voice!r} is not a Voxtral preset: {known}")
        self.voice_id = matches[0]
        logger.info("mistral: %s", self.config)
        return self.voice_id

    @asynccontextmanager
    async def open_legs(self, call: CallRuntime) -> AsyncIterator[tuple[SpeechToText, TextToSpeech]]:
        voice_id = self.voice_id or await self.resolve_voice()
        url = f"{STT_URL}?{urlencode({'model': self.config.stt_model})}"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with self.connect(url, headers) as ws:
            stt = VoxtralRealtime(ws, call.setup.turn_taking.silence_ms)
            await stt.start()
            yield stt, VoxtralTTS(self.client, self.config.tts_model, voice_id)


PROVIDER = MistralProvider
