"""Mistral: Voxtral Realtime frames, the local end of turn, the chat round and Voxtral TTS, offline.

The parity matrix (``test_parity.py``) covers the call through the shared
cascade; these pin Mistral's own legs.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

pytest.importorskip("mistralai")

from vamos_voice.providers.mistral import (
    MistralChat,
    MistralConfig,
    MistralProvider,
    VoxtralRealtime,
    VoxtralTTS,
    float32_to_pcm16,
)

from .cascade_fakes import SILENCE
from .fakes import (
    HELLO,
    REMI,
    FakeLine,
    FakeVendorSocket,
    ScriptedSession,
    runtime,
)

SPEECH = b"\x01\x00" * 480  # 20 ms


def test_config_comes_from_the_environment_with_the_energy_defaults() -> None:
    assert MistralProvider.required_env == ("MISTRAL_API_KEY",)
    config = MistralProvider.from_env({"MISTRAL_API_KEY": "k"}).config
    assert (config.llm_model, config.stt_model, config.tts_model, config.voice) == (
        "mistral-medium-2604",
        "voxtral-mini-transcribe-realtime-2602",
        "voxtral-mini-tts-2603",
        "en_paul_neutral",
    )
    assert "k" not in repr(config)
    custom = MistralConfig.from_env(
        {
            "MISTRAL_API_KEY": "k",
            "VAMOS_MISTRAL_LLM_MODEL": "l",
            "VAMOS_MISTRAL_STT_MODEL": "s",
            "VAMOS_MISTRAL_TTS_MODEL": "t",
            "VAMOS_MISTRAL_VOICE": "v",
        }
    )
    assert (custom.llm_model, custom.stt_model, custom.tts_model, custom.voice) == ("l", "s", "t", "v")


async def test_voxtral_realtime_declares_16_khz_and_flushes_after_the_silence() -> None:
    sock = FakeVendorSocket()
    stt = VoxtralRealtime(sock, silence_ms=800)
    sock.push({"type": "session.created", "session": {"request_id": "r"}})
    await stt.start()
    assert sock.sent == [
        {
            "type": "session.update",
            "session": {"audio_format": {"encoding": "pcm_s16le", "sample_rate": 16000}},
        }
    ]
    for _ in range(10):
        await stt.feed(SPEECH, speaking=True)
    for _ in range(39):
        await stt.feed(SILENCE, speaking=False)
    assert not any(f["type"] == "input_audio.flush" for f in sock.sent)
    await stt.feed(SILENCE, speaking=False)  # 800 ms of silence: the turn is over
    appends = [f for f in sock.sent if f["type"] == "input_audio.append"]
    assert len(appends) == 50
    assert sum(len(base64.b64decode(f["audio"])) for f in appends) == pytest.approx(50 * 640, abs=4)
    assert sock.sent[-1] == {"type": "input_audio.flush"}


async def test_voxtral_realtime_turns_are_the_flushed_transcripts() -> None:
    sock = FakeVendorSocket()
    stt = VoxtralRealtime(sock, silence_ms=800)
    turns: list[str] = []

    async def on_turn(text: str) -> None:
        turns.append(text)

    sock.push({"type": "transcription.text.delta", "text": "Hel"})
    sock.push({"type": "transcription.done", "text": "Hello? Yes, this is Marcus."})
    sock.push({"type": "error", "error": {"message": "boom", "code": 500}})
    interim: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        await stt.run(on_turn, interim.append)
    assert turns == ["Hello? Yes, this is Marcus."]
    assert interim == ["Hel"] and VoxtralRealtime.interim_text


async def test_voxtral_realtime_refuses_a_session_that_did_not_open() -> None:
    sock = FakeVendorSocket()
    sock.push({"type": "error", "error": {"message": "invalid model", "code": 400}})
    with pytest.raises(RuntimeError, match="invalid model"):
        await VoxtralRealtime(sock, 800).start()


class FakeMistral:
    """The slice of the ``mistralai`` client the provider uses."""

    def __init__(self, message: Any = None, voices: list[Any] | None = None, audio: list[bytes] = ()) -> None:
        self.chat_requests: list[dict[str, Any]] = []
        self.speech_requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(complete_async=self._complete)
        self.audio = SimpleNamespace(
            speech=SimpleNamespace(complete_async=self._speech),
            voices=SimpleNamespace(list_async=self._voices),
        )
        self._message, self._voices_list, self._audio = message, voices or [], list(audio)

    async def _complete(self, **kwargs: Any) -> Any:
        self.chat_requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])

    async def _speech(self, **kwargs: Any) -> Any:
        self.speech_requests.append(kwargs)

        async def events():
            for chunk in self._audio:
                yield SimpleNamespace(
                    data=SimpleNamespace(
                        type="speech.audio.delta", audio_data=base64.b64encode(chunk).decode()
                    )
                )
            yield SimpleNamespace(data=SimpleNamespace(type="speech.audio.done"))

        return events()

    async def _voices(self, **kwargs: Any) -> Any:
        return SimpleNamespace(items=self._voices_list)


async def test_a_chat_round_yields_the_text_then_the_tool_calls() -> None:
    call = SimpleNamespace(id="abc123def", function=SimpleNamespace(name="get_account", arguments={"x": 1}))
    client = FakeMistral(SimpleNamespace(content="One moment.", tool_calls=[call]))
    tools = [{"type": "function", "function": {"name": "get_account", "description": "", "parameters": {}}}]
    parts = [
        p
        async for p in MistralChat(client, "mistral-medium-2604").reply(
            [{"role": "user", "content": "hi"}], tools
        )
    ]  # type: ignore[arg-type]
    assert parts[0] == "One moment."
    assert (parts[1].id, parts[1].name, json.loads(parts[1].arguments)) == (
        "abc123def",
        "get_account",
        {"x": 1},
    )
    (request,) = client.chat_requests
    assert request["model"] == "mistral-medium-2604" and request["tool_choice"] == "auto"
    assert request["tools"] == tools


async def test_voxtral_tts_speaks_the_whole_round_as_pcm16() -> None:
    f32 = np.array([0.0, 0.5, -0.5, 1.5], dtype="<f4").tobytes()
    client = FakeMistral(audio=[f32])
    tts = VoxtralTTS(client, "voxtral-mini-tts-2603", "voice-uuid")  # type: ignore[arg-type]

    async def text():
        for delta in ("Hello ", "there."):
            yield delta

    audio = [pcm async for pcm in tts.speak(text())]
    assert audio == [np.array([0, 16383, -16383, 32767], dtype="<i2").tobytes()]
    (request,) = client.speech_requests
    assert request == {
        "model": "voxtral-mini-tts-2603",
        "input": "Hello there.",
        "voice_id": "voice-uuid",
        "response_format": "pcm",
        "stream": True,
    }
    assert float32_to_pcm16(b"") == b""


async def test_the_voice_slug_resolves_at_startup_and_an_unknown_one_fails() -> None:
    voices = [SimpleNamespace(slug="en_paul_neutral", id="uuid-1"), SimpleNamespace(slug="fr_x", id="uuid-2")]
    provider = MistralProvider(MistralConfig(api_key="k"), client=FakeMistral(voices=voices))  # type: ignore[arg-type]
    assert await provider.resolve_voice() == "uuid-1"
    wrong = MistralProvider(MistralConfig(api_key="k", voice="nope"), client=FakeMistral(voices=voices))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="nope"):
        await wrong.resolve_voice()


async def test_the_stt_socket_carries_the_key_in_a_header_and_the_model_in_the_url() -> None:
    sock = FakeVendorSocket()
    voices = [SimpleNamespace(slug="en_paul_neutral", id="uuid-1")]
    provider = MistralProvider(MistralConfig(api_key="secret-k"), client=FakeMistral(voices=voices))  # type: ignore[arg-type]
    provider.connect = sock.connector()
    sock.push({"type": "session.created"})
    call = runtime(REMI, FakeLine(), ScriptedSession())
    async with provider.open_legs(call) as (stt, tts):
        assert isinstance(stt, VoxtralRealtime) and isinstance(tts, VoxtralTTS)
    assert (
        sock.url
        == "wss://api.mistral.ai/v1/audio/transcriptions/realtime?model=voxtral-mini-transcribe-realtime-2602"
    )
    assert sock.headers == {"Authorization": "Bearer secret-k"}
    assert HELLO  # the parity matrix drives a whole call through these legs
