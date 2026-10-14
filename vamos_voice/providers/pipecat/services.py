"""Pipecat's speech services, chosen by env: one small factory per option, no branching elsewhere.

Every VAmoS call is in English, so each service that takes a language has it pinned to English
(configuration, as the other providers do; OpenAI's speech endpoint takes none).

Reliability (not behaviour): OpenAI's speech endpoint sometimes holds a request for 25 s or more
before its first byte, while the same request retried answers in about half a second. Pipecat
builds its OpenAI client with the SDK's defaults (a 600 s timeout, 2 retries) and abandons a
reply after 3 s without audio, so one stalled request made the agent mute for the whole turn.
``RetryingOpenAITTS`` gives each attempt ``TTS_ATTEMPT_TIMEOUT_S`` and retries a stall, a 5xx,
a 429 or a dropped connection ``TTS_MAX_RETRIES`` times (the SDK's backoff), all within the
shared ``RETRY_BUDGET_S``; Pipecat waits that long for the audio before giving up on a reply.
Voice, model and text are unchanged."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import RETRY_BUDGET_S

#: One speech request's time to answer (and between audio chunks); a healthy one starts in ~0.5 s.
TTS_ATTEMPT_TIMEOUT_S = 3.0
#: 5 attempts of 3 s plus the SDK's backoff (0.5 s doubling: 7.5 s at most) fit ``RETRY_BUDGET_S``.
TTS_MAX_RETRIES = 4
#: How long Pipecat waits for a reply's audio before giving up on it (its default is 3 s).
TTS_IDLE_TIMEOUT_S = RETRY_BUDGET_S + TTS_ATTEMPT_TIMEOUT_S


class RetryingOpenAITTS(OpenAITTSService):
    """Pipecat's OpenAI TTS with a per-attempt timeout and retries within ``RETRY_BUDGET_S``.

    Pipecat's constructor offers no ``max_retries``, so its client is re-optioned once here.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(stop_frame_timeout_s=TTS_IDLE_TIMEOUT_S, **kwargs)
        self._client = self._client.with_options(timeout=TTS_ATTEMPT_TIMEOUT_S, max_retries=TTS_MAX_RETRIES)


@dataclass(frozen=True)
class ServiceConfig:
    """Keys and model choices for the cascaded pipeline (read once, at startup)."""

    openai_api_key: str = field(repr=False)
    stt: str
    tts: str
    deepgram_api_key: str | None = field(default=None, repr=False)
    deepgram_model: str = "nova-3-general"
    openai_stt_model: str = "gpt-4o-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "marin"
    cartesia_api_key: str | None = field(default=None, repr=False)
    cartesia_voice_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ServiceConfig:
        stt = env.get("VAMOS_PIPECAT_STT") or "deepgram"
        tts = env.get("VAMOS_PIPECAT_TTS") or "openai"
        if stt not in STT_FACTORIES or tts not in TTS_FACTORIES:
            raise ValueError(
                f"VAMOS_PIPECAT_STT must be one of {sorted(STT_FACTORIES)}, VAMOS_PIPECAT_TTS one of {sorted(TTS_FACTORIES)}"
            )
        needed = {
            "deepgram": ["DEEPGRAM_API_KEY"],
            "cartesia": ["CARTESIA_API_KEY", "VAMOS_CARTESIA_VOICE_ID"],
        }
        missing = [k for choice in (stt, tts) for k in needed.get(choice, []) if not env.get(k)]
        if missing:
            raise ValueError(f"pipecat with STT={stt}, TTS={tts} needs {', '.join(missing)}")
        return cls(
            openai_api_key=env["OPENAI_API_KEY"],
            stt=stt,
            tts=tts,
            deepgram_api_key=env.get("DEEPGRAM_API_KEY"),
            deepgram_model=env.get("VAMOS_DEEPGRAM_MODEL") or cls.deepgram_model,
            openai_stt_model=env.get("VAMOS_OPENAI_STT_MODEL") or cls.openai_stt_model,
            openai_tts_model=env.get("VAMOS_OPENAI_TTS_MODEL") or cls.openai_tts_model,
            openai_tts_voice=env.get("VAMOS_OPENAI_VOICE") or cls.openai_tts_voice,
            cartesia_api_key=env.get("CARTESIA_API_KEY"),
            cartesia_voice_id=env.get("VAMOS_CARTESIA_VOICE_ID"),
        )


def _deepgram(c: ServiceConfig) -> FrameProcessor:
    assert c.deepgram_api_key
    return DeepgramSTTService(
        api_key=c.deepgram_api_key,
        sample_rate=CHANNEL_RATE_HZ,
        settings=DeepgramSTTService.Settings(model=c.deepgram_model, language=Language.EN),
    )


def _openai_stt(c: ServiceConfig) -> FrameProcessor:
    return OpenAISTTService(
        api_key=c.openai_api_key,
        settings=OpenAISTTService.Settings(model=c.openai_stt_model, language=Language.EN),
    )


def _openai_tts(c: ServiceConfig) -> FrameProcessor:
    return RetryingOpenAITTS(
        api_key=c.openai_api_key,
        sample_rate=CHANNEL_RATE_HZ,
        settings=OpenAITTSService.Settings(model=c.openai_tts_model, voice=c.openai_tts_voice),
    )


def _cartesia(c: ServiceConfig) -> FrameProcessor:
    assert c.cartesia_api_key and c.cartesia_voice_id
    return CartesiaTTSService(
        api_key=c.cartesia_api_key,
        sample_rate=CHANNEL_RATE_HZ,
        settings=CartesiaTTSService.Settings(voice=c.cartesia_voice_id, language=Language.EN),
    )


STT_FACTORIES: dict[str, Callable[[ServiceConfig], FrameProcessor]] = {
    "deepgram": _deepgram,
    "openai": _openai_stt,
}
TTS_FACTORIES: dict[str, Callable[[ServiceConfig], FrameProcessor]] = {
    "openai": _openai_tts,
    "cartesia": _cartesia,
}
