"""The agent on xAI's streaming STT and TTS, cascaded in-pod around a chat model.

``grok_voice`` runs xAI's speech-to-speech model; this provider keeps the
three legs apart so xAI's STT and TTS can be scored on the same LLM as the
other cascades, as on the VAmoS Energy "Grok STT + TTS cascade" candidates
(rory-agent ``grok-cascade/``). The pipeline is the shared cascade
(``vamos_voice.providers.cascade``); the legs:

- **STT**: ``wss://api.x.ai/v1/stt`` (``wire.connect_websocket``), the
  caller's 24 kHz PCM16 sent unconverted as binary frames, ``language=en``,
  ``format=true`` (spoken numbers come back as digits). **End of turn is
  xAI's**: Smart Turn (``smart_turn=0.7``, xAI's setting for callers reading
  out numbers; ``smart_turn_timeout=3000`` ms closes a turn after that much
  silence regardless). Only an utterance final (``speech_final``) is a turn;
  it restates the whole utterance. Silero (local) drives barge-in only.
- **LLM**: ``VAMOS_GROK_CASCADE_LLM``: ``gpt-4.1-mini`` (default, the other
  cascades' model, on OpenAI: needs ``OPENAI_API_KEY``) or ``grok-4.3`` (on
  xAI's OpenAI-compatible API, ``reasoning_effort: "none"``). Streamed through
  the ``openai`` SDK (``openai_chat.OpenAIChat``).
- **TTS**: ``wss://api.x.ai/v1/tts``, ``codec=pcm``, 24 kHz, voice
  ``VAMOS_GROK_VOICE`` (default ``carina``). Every LLM token goes up as a
  ``text.delta`` as it arrives and xAI does the chunking, so speech starts on
  the reply's first words; ``text.done`` ends the utterance. A barge-in sends
  ``text.clear`` and drops audio up to the ``audio.clear`` that acknowledges
  it, so the next reply starts clean on the same socket.

``XAI_API_KEY`` is required (STT and TTS, and the LLM when it is Grok); an
unknown ``VAMOS_GROK_CASCADE_LLM`` or a missing key for it stops startup.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlencode

import websockets

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import CallRuntime
from vamos_voice.providers.cascade import CascadeProvider, SpeechToText, TextToSpeech, TurnSink
from vamos_voice.providers.openai_chat import OpenAIChat, openai_client

logger = logging.getLogger(__name__)

STT_URL = "wss://api.x.ai/v1/stt"
TTS_URL = "wss://api.x.ai/v1/tts"
#: Smart Turn: only an end-of-turn score above this ends the caller's turn (xAI's value for number reading).
SMART_TURN = 0.7
SMART_TURN_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class LlmRoute:
    """Where a cascade model runs: base URL, the key's env var, extra request fields."""

    base_url: str
    key_env: str
    extra: Mapping[str, Any] = field(default_factory=dict)


#: grok-4.3 is xAI's last model with a non-reasoning mode; ``none`` is that mode.
LLM_ROUTES = {
    "gpt-4.1-mini": LlmRoute("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "grok-4.3": LlmRoute("https://api.x.ai/v1", "XAI_API_KEY", {"reasoning_effort": "none"}),
}


@dataclass(frozen=True)
class GrokCascadeConfig:
    xai_api_key: str = field(repr=False)
    llm_api_key: str = field(repr=False)
    llm_model: str = "gpt-4.1-mini"
    stt_model: str = "grok-voice-transcribe-2.0"
    voice: str = "carina"

    @property
    def route(self) -> LlmRoute:
        return LLM_ROUTES[self.llm_model]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GrokCascadeConfig:
        model = env.get("VAMOS_GROK_CASCADE_LLM") or cls.llm_model
        if model not in LLM_ROUTES:
            raise ValueError(f"VAMOS_GROK_CASCADE_LLM must be one of {sorted(LLM_ROUTES)}, got {model!r}")
        key_env = LLM_ROUTES[model].key_env
        if not env.get(key_env):
            raise ValueError(f"grok_cascade with VAMOS_GROK_CASCADE_LLM={model} needs {key_env}")
        return cls(
            xai_api_key=env["XAI_API_KEY"],
            llm_api_key=env[key_env],
            llm_model=model,
            stt_model=env.get("VAMOS_GROK_STT_MODEL") or cls.stt_model,
            voice=env.get("VAMOS_GROK_VOICE") or cls.voice,
        )

    def stt_url(self) -> str:
        query = {
            "model": self.stt_model,
            "encoding": "pcm",
            "sample_rate": CHANNEL_RATE_HZ,
            "language": "en",
            "format": "true",
            "smart_turn": SMART_TURN,
            "smart_turn_timeout": SMART_TURN_TIMEOUT_MS,
        }
        return f"{STT_URL}?{urlencode(query)}"

    def tts_url(self) -> str:
        query = {"voice": self.voice, "language": "en", "codec": "pcm", "sample_rate": CHANNEL_RATE_HZ}
        return f"{TTS_URL}?{urlencode(query)}"


class GrokSTT(SpeechToText):
    """xAI streaming STT; its Smart Turn decides where the caller's turn ends."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def start(self) -> None:
        """xAI: wait for ``transcript.created`` before sending audio."""
        created = json.loads(await self.ws.recv())
        if created.get("type") != "transcript.created":
            raise RuntimeError(f"xAI STT opened with {created.get('type')!r}: {created.get('message')}")

    async def feed(self, pcm: bytes, speaking: bool) -> None:
        await self.ws.send(pcm)

    async def run(self, on_turn: TurnSink) -> None:
        while True:
            try:
                event = json.loads(await self.ws.recv())
            except websockets.ConnectionClosed as closed:
                logger.info("xAI STT closed: %s", closed)
                return
            kind = event.get("type")
            if kind == "transcript.partial" and event.get("speech_final"):
                await on_turn(event.get("text") or "")
            elif kind == "error":
                raise RuntimeError(f"xAI STT error: {event.get('message')}")


class GrokTTS(TextToSpeech):
    """xAI streaming TTS on one socket for the call, one utterance per LLM round."""

    ms_per_word = 305  # measured on carina by the Energy candidate

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._sender: asyncio.Task[None] | None = None

    async def _send_text(self, text: AsyncIterator[str]) -> None:
        async for delta in text:
            await self.ws.send(json.dumps({"type": "text.delta", "delta": delta}))
        await self.ws.send(json.dumps({"type": "text.done"}))

    async def speak(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        self._sender = asyncio.create_task(self._send_text(text), name="text->xai-tts")
        carry = b""  # xAI cuts PCM at arbitrary bytes: a stray byte leads the next chunk
        try:
            while True:
                event = json.loads(await self.ws.recv())
                kind = event.get("type")
                if kind == "audio.delta":
                    pcm = carry + base64.b64decode(event.get("delta") or "")
                    whole = len(pcm) - len(pcm) % 2
                    pcm, carry = pcm[:whole], pcm[whole:]
                    if pcm:
                        yield pcm
                elif kind == "audio.done":
                    sender, self._sender = self._sender, None
                    await sender  # the utterance is whole: its text has all gone up; a failed send fails it
                    return
                elif kind == "error":
                    raise RuntimeError(f"xAI TTS error: {event.get('message')}")
        finally:
            await self._stop_sender()  # abandoned (barge-in, error, end of call): the rest never goes up

    async def _stop_sender(self) -> None:
        sender, self._sender = self._sender, None
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def clear(self) -> None:
        """Stop the utterance; audio already in flight is dropped up to xAI's ``audio.clear``."""
        await self._stop_sender()
        await self.ws.send(json.dumps({"type": "text.clear"}))
        while json.loads(await self.ws.recv()).get("type") != "audio.clear":
            pass


class GrokCascadeProvider(CascadeProvider):
    name = "grok_cascade"
    required_env = ("XAI_API_KEY",)

    def __init__(self, config: GrokCascadeConfig) -> None:
        self.config = config
        route = config.route
        self.llm = OpenAIChat(
            openai_client(route.base_url, config.llm_api_key), config.llm_model, route.extra
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(GrokCascadeConfig.from_env(env))

    @asynccontextmanager
    async def open_legs(self, call: CallRuntime) -> AsyncIterator[tuple[SpeechToText, TextToSpeech]]:
        headers = {"Authorization": f"Bearer {self.config.xai_api_key}"}
        async with (
            self.connect(self.config.stt_url(), headers) as stt_ws,
            self.connect(self.config.tts_url(), headers) as tts_ws,
        ):
            stt = GrokSTT(stt_ws)
            await stt.start()
            yield stt, GrokTTS(tts_ws)


PROVIDER = GrokCascadeProvider
