"""Remi on the OpenAI Realtime API (speech-to-speech, GA protocol).

Everything protocol-level is ``RealtimeProtocolProvider``; this file is the
OpenAI endpoint and session. Audio is PCM16 24 kHz both ways, the channel's
own rate, so nothing is resampled. Bench candidate: ``trace_adapter:
openai-realtime``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from remi_core.providers.base import SessionSetup
from remi_core.providers.realtime_protocol import RATE_HZ, RealtimeProtocolProvider

DEFAULT_MODEL = "gpt-realtime"
DEFAULT_VOICE = "marin"
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


class OpenAIRealtimeProvider(RealtimeProtocolProvider):
    name = "openai_realtime"
    trace_adapter = "openai-realtime"
    required_env = ("OPENAI_API_KEY",)

    def __init__(self, api_key: str, model: str, voice: str, transcribe_model: str) -> None:
        self._api_key = api_key
        self.model, self.voice, self.transcribe_model = model, voice, transcribe_model

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["OPENAI_API_KEY"],
            model=env.get("REMI_OPENAI_REALTIME_MODEL") or DEFAULT_MODEL,
            voice=env.get("REMI_OPENAI_VOICE") or DEFAULT_VOICE,
            transcribe_model=env.get("REMI_OPENAI_TRANSCRIBE_MODEL") or DEFAULT_TRANSCRIBE_MODEL,
        )

    def url(self) -> str:
        return f"wss://api.openai.com/v1/realtime?model={self.model}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def session_config(self, setup: SessionSetup) -> dict[str, Any]:
        turns = setup.turn_taking
        pcm = {"type": "audio/pcm", "rate": RATE_HZ}
        return {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": setup.instructions,
            "audio": {
                "input": {
                    "format": pcm,
                    "transcription": {"model": self.transcribe_model},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": turns.vad_threshold,
                        "prefix_padding_ms": turns.prefix_padding_ms,
                        "silence_duration_ms": turns.silence_ms,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {"format": pcm, "voice": self.voice},
            },
            "tools": self.tool_format.convert(setup.tools),
            "tool_choice": "auto",
        }


PROVIDER = OpenAIRealtimeProvider
