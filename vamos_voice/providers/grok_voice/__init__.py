"""The agent on xAI's Grok Voice Agent API (speech-to-speech), a dialect of the Realtime protocol.

Everything protocol-level is ``RealtimeProtocolProvider``; this file is the
xAI endpoint, the session shape and the capability where Grok differs from
OpenAI. Audio is PCM16 24 kHz both ways, the channel's own rate, so
nothing is resampled. Bench candidate: ``trace_adapter: openai-realtime`` (the
bench's Realtime adapter reads Grok's frames, including its top-level
``turn_detection`` and its missing ``output_item.added``).

How Grok differs, and what this provider does about it (docs.x.ai, "Voice
Agent API"; behaviour observed against ``grok-voice-think-fast-2.0`` in
veris-ai/rory-agent ``grok-voice/README.md``):

- **Session shape.** ``voice``, ``instructions`` and ``turn_detection`` sit at
  the session's top level, not under ``audio``.
- **Caller transcription** is emitted only when
  ``audio.input.transcription.model`` is ``grok-transcribe``; without it the
  caller's words never reach the judge's trace. Always sent.
- **Turn detection** accepts ``type`` and ``silence_duration_ms`` (plus
  ``idle_timeout_ms``); ``threshold`` and ``prefix_padding_ms`` are not part
  of the documented session, so only ``TurnTaking.silence_ms`` is mapped and
  the rest stay on xAI's defaults.
- **No forced tool choice.** ``response.create`` documents only
  ``instructions``. Nothing here needs one: the connect-time reads are the
  agent's screen pop, already in the session instructions.
- **No truncation.** xAI lists ``conversation.item.truncate`` and
  ``response.cancel`` as unsupported. Under server VAD Grok stops generating
  on its own when the caller speaks, and the model's context ends where
  generation stopped, which runs ahead of what the caller heard (Grok streams
  faster than real time). So on barge-in this provider sends nothing and only
  marks the queued playout as interrupted; the context may hold up to a few
  seconds of words the caller never heard.
- **Turn taking** is Grok's own: its server VAD answers and interrupts.
- **Opening turn** (``agent_first``): ``response.create`` with per-response
  ``instructions``, as for OpenAI; that field is documented.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from vamos_voice.providers.base import SessionSetup
from vamos_voice.providers.realtime_protocol import RATE_HZ, RealtimeProtocolProvider

DEFAULT_MODEL = "grok-voice-think-fast-2.0"
DEFAULT_VOICE = "eve"
#: The only model xAI documents for input transcription; without it no caller transcript is emitted.
TRANSCRIBE_MODEL = "grok-transcribe"
#: The standard xAI name, as on the other VAmoS benches; Veris Bench allows this exact
#: candidate-env key (other XAI_* / GROK_* names stay reserved for its harnesses).
API_KEY_ENV = "XAI_API_KEY"


class GrokVoiceProvider(RealtimeProtocolProvider):
    name = "grok_voice"
    trace_adapter = "openai-realtime"
    required_env = (API_KEY_ENV,)
    truncates_on_barge_in = False

    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self._api_key = api_key
        self.model, self.voice = model, voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env[API_KEY_ENV],
            model=env.get("VAMOS_GROK_MODEL") or DEFAULT_MODEL,
            voice=env.get("VAMOS_GROK_VOICE") or DEFAULT_VOICE,
        )

    def url(self) -> str:
        return f"wss://api.x.ai/v1/realtime?model={self.model}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def session_config(self, setup: SessionSetup) -> dict[str, Any]:
        pcm = {"type": "audio/pcm", "rate": RATE_HZ}
        return {
            "voice": self.voice,
            "instructions": setup.instructions,
            "turn_detection": {"type": "server_vad", "silence_duration_ms": setup.turn_taking.silence_ms},
            "audio": {
                "input": {"format": pcm, "transcription": {"model": TRANSCRIBE_MODEL}},
                "output": {"format": pcm},
            },
            "tools": self.tool_format.convert(setup.tools),
        }


PROVIDER = GrokVoiceProvider
