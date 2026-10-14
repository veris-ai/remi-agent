"""The agent on Hugging Face hosted inference: the huggingface/speech-to-speech cascade, every leg on HF.

As on the VAmoS Energy Hugging Face candidate (rory-agent ``huggingface/``),
serverless Inference Providers, one ``HF_TOKEN`` for all three legs; the
pipeline is the shared in-pod cascade (``vamos_voice.providers.cascade``):

- **STT**: Whisper (``VAMOS_HF_STT_MODEL``, default ``openai/whisper-large-v3``)
  on ``hf-inference``. It is request/response, so the leg buffers each caller
  utterance: Silero opens it (with ``PREROLL_MS`` of audio from before the
  onset, which the detector trips a window late on), ``TurnTaking.silence_ms``
  of non-speech ends it (``SilenceEndpoint``; HF has no endpointing), and the
  utterance goes up as a 16 kHz WAV with ``language=en`` pinned (left to
  detect, Whisper wrote accented callers in other scripts). A request that
  times out drops that one turn, not the call.
- **LLM**: ``VAMOS_HF_LLM_MODEL`` (default ``openai/gpt-oss-120b:groq``, the
  Energy candidate's) on the router's chat completions, through the ``openai``
  SDK (``openai_chat.OpenAIChat``) so the bench traces it.
- **TTS**: ``VAMOS_HF_TTS_MODEL`` (default ``hexgrad/Kokoro-82M``, voice
  ``VAMOS_HF_TTS_VOICE``, default ``af_heart``) through the router's fal-ai
  route, resolved from the Hub's provider mapping at startup (an unservable
  model fails the boot). fal returns a signed URL to the whole WAV, fetched
  without the token; the cascade paces it out so a barge-in can still cut it.
  Startup synthesizes once before ``/health`` says ready: fal parks an idle
  Kokoro worker, and the first synthesis after that took 20-60 s.

The HTTP legs use ``httpx`` with 429/503 waited out (bursts, cold models) for
up to ``RETRY_MAX_S``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import wave
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Self

import httpx
import numpy as np

from vamos_voice.audio import CHANNEL_RATE_HZ, Resampler, pcm_ms
from vamos_voice.providers.base import CallRuntime
from vamos_voice.providers.cascade import (
    CascadeProvider,
    SilenceEndpoint,
    SpeechToText,
    TextToSpeech,
    TurnSink,
)
from vamos_voice.providers.openai_chat import OpenAIChat, openai_client

logger = logging.getLogger(__name__)

ROUTER = "https://router.huggingface.co"
HUB_API = "https://huggingface.co/api/models"
STT_RATE_HZ = 16_000
#: Audio kept from just before Silero's onset, so the transcript keeps the first syllable.
PREROLL_MS = 240
#: Sized for provider cold starts (a parked fal worker took 20-60 s), not for a healthy request.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
#: Whisper requests are a caller's turn: past this the turn is dropped rather than the call held.
STT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RETRY_STATUSES = {429, 503}
RETRY_MAX_S = 25.0
RETRY_FIRST_S = 0.5


@dataclass(frozen=True)
class HuggingFaceConfig:
    token: str = field(repr=False)
    llm_model: str = "openai/gpt-oss-120b:groq"
    stt_model: str = "openai/whisper-large-v3"
    tts_model: str = "hexgrad/Kokoro-82M"
    tts_voice: str = "af_heart"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> HuggingFaceConfig:
        return cls(
            token=env["HF_TOKEN"],
            llm_model=env.get("VAMOS_HF_LLM_MODEL") or cls.llm_model,
            stt_model=env.get("VAMOS_HF_STT_MODEL") or cls.stt_model,
            tts_model=env.get("VAMOS_HF_TTS_MODEL") or cls.tts_model,
            tts_voice=env.get("VAMOS_HF_TTS_VOICE") or cls.tts_voice,
        )


async def post_with_retry(http: httpx.AsyncClient, url: str, label: str, **kwargs: Any) -> httpx.Response:
    """POST, waiting out 429 bursts and cold models (503) for up to ``RETRY_MAX_S``."""
    delay, started = RETRY_FIRST_S, time.monotonic()
    while True:
        response = await http.post(url, **kwargs)
        waited = time.monotonic() - started
        if response.status_code not in RETRY_STATUSES or waited + delay > RETRY_MAX_S:
            if response.is_error:  # raise_for_status drops the body, where providers put the reason
                logger.error("%s: HTTP %d: %s", label, response.status_code, response.text[:300])
            response.raise_for_status()
            return response
        logger.warning("%s: HTTP %d, retrying in %.1fs", label, response.status_code, delay)
        await asyncio.sleep(delay)
        delay *= 2


def pcm_to_wav(pcm: bytes, rate_hz: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes) -> bytes:
    """A WAV as the caller's format: PCM16 mono at 24 kHz."""
    with wave.open(io.BytesIO(data), "rb") as w:
        frames = w.readframes(w.getnframes())
        width, channels, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
    if width != 2:
        raise RuntimeError(f"TTS returned {8 * width}-bit audio; PCM16 expected")
    samples = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype("<i2")
    return Resampler(rate, CHANNEL_RATE_HZ).process(samples.tobytes())


class WhisperSTT(SpeechToText):
    """Buffered utterances, ended by local silence, transcribed one request each."""

    def __init__(self, http: httpx.AsyncClient, url: str, silence_ms: int) -> None:
        self.http, self.url = http, url
        self.endpoint = SilenceEndpoint(silence_ms)
        self.preroll: deque[bytes] = deque()
        self.preroll_ms = 0.0
        self.utterance: list[bytes] = []
        self.utterances: asyncio.Queue[bytes] = asyncio.Queue()

    async def feed(self, pcm: bytes, speaking: bool) -> None:
        if not self.endpoint.in_turn and not speaking:
            self.preroll.append(pcm)
            self.preroll_ms += pcm_ms(len(pcm))
            while self.preroll and self.preroll_ms - pcm_ms(len(self.preroll[0])) >= PREROLL_MS:
                self.preroll_ms -= pcm_ms(len(self.preroll.popleft()))
            return
        if not self.endpoint.in_turn:
            self.utterance = [*self.preroll]
            self.preroll.clear()
            self.preroll_ms = 0.0
        self.utterance.append(pcm)
        if self.endpoint.push(len(pcm), speaking):
            self.utterances.put_nowait(b"".join(self.utterance))
            self.utterance = []

    async def run(self, on_turn: TurnSink) -> None:
        while True:
            await on_turn(await self.transcribe(await self.utterances.get()))

    async def transcribe(self, pcm: bytes) -> str:
        wav = pcm_to_wav(Resampler(CHANNEL_RATE_HZ, STT_RATE_HZ).process(pcm), STT_RATE_HZ)
        body = {
            "inputs": base64.b64encode(wav).decode(),
            "parameters": {"generate_kwargs": {"language": "en", "task": "transcribe"}},
        }
        try:
            response = await post_with_retry(self.http, self.url, "stt", json=body, timeout=STT_TIMEOUT)
        except httpx.TimeoutException:
            logger.warning(
                "stt: no transcript for %.1fs of audio; the turn is dropped", pcm_ms(len(pcm)) / 1000
            )
            return ""
        return str(response.json().get("text") or "")


class KokoroTTS(TextToSpeech):
    """Whole-reply synthesis on fal-ai: a signed URL to a WAV, fetched without the HF token."""

    def __init__(self, http: httpx.AsyncClient, download: httpx.AsyncClient, url: str, voice: str) -> None:
        self.http, self.download, self.url, self.voice = http, download, url, voice

    async def synthesize(self, text: str) -> bytes:
        response = await post_with_retry(self.http, self.url, "tts", json={"text": text, "voice": self.voice})
        audio = await self.download.get(response.json()["audio"]["url"])
        audio.raise_for_status()
        return wav_to_pcm(audio.content)

    async def speak(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        whole = "".join([delta async for delta in text]).strip()
        if whole:
            yield await self.synthesize(whole)


class HuggingFaceProvider(CascadeProvider):
    name = "huggingface"
    required_env = ("HF_TOKEN",)

    def __init__(
        self,
        config: HuggingFaceConfig,
        http: httpx.AsyncClient | None = None,
        download: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.http = http or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.token}"}, timeout=TIMEOUT
        )
        self.download = download or httpx.AsyncClient(timeout=TIMEOUT)  # signed URLs: no token leaves HF
        self.llm = OpenAIChat(openai_client(f"{ROUTER}/v1", config.token), config.llm_model)
        self.tts_url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(HuggingFaceConfig.from_env(env))

    @property
    def stt_url(self) -> str:
        return f"{ROUTER}/hf-inference/models/{self.config.stt_model}"

    async def resolve_tts_route(self) -> str:
        """The model's live fal-ai text-to-speech route (the request shape here is fal's)."""
        response = await self.http.get(
            f"{HUB_API}/{self.config.tts_model}", params={"expand": "inferenceProviderMapping"}
        )
        response.raise_for_status()
        mapping = response.json().get("inferenceProviderMapping") or {}
        if isinstance(mapping, list):  # the Hub has served both shapes
            mapping = {entry.get("provider"): entry for entry in mapping}
        live = {
            provider: entry["providerId"]
            for provider, entry in mapping.items()
            if entry.get("task") == "text-to-speech" and entry.get("status") == "live"
        }
        if "fal-ai" not in live:
            raise RuntimeError(
                f"{self.config.tts_model!r} has no live fal-ai text-to-speech route: {sorted(live)}"
            )
        self.tts_url = f"{ROUTER}/fal-ai/{live['fal-ai']}"
        return self.tts_url

    async def warm_up(self) -> None:
        await super().warm_up()
        tts = KokoroTTS(self.http, self.download, await self.resolve_tts_route(), self.config.tts_voice)
        started = time.monotonic()
        await tts.synthesize("Hello.")
        logger.info("huggingface: %s; TTS warm in %.1fs", self.config, time.monotonic() - started)

    @asynccontextmanager
    async def open_legs(self, call: CallRuntime) -> AsyncIterator[tuple[WhisperSTT, KokoroTTS]]:
        tts_url = self.tts_url or await self.resolve_tts_route()
        yield (
            WhisperSTT(self.http, self.stt_url, call.setup.turn_taking.silence_ms),
            KokoroTTS(self.http, self.download, tts_url, self.config.tts_voice),
        )


PROVIDER = HuggingFaceProvider
