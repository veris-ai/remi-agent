"""Grok Voice: the session shape and barge-in, against what the provider sends.

The parity matrix (``test_parity.py``) covers the call as a whole; these pin
the places Grok's dialect differs from OpenAI Realtime, so a change to the
shared ``RealtimeProtocolProvider`` cannot silently change one vendor.
"""

from __future__ import annotations

import asyncio
import base64

from vamos_voice.providers.base import CallRuntime
from vamos_voice.providers.grok_voice import API_KEY_ENV, GrokVoiceProvider

from .fakes import REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime
from .test_parity import GrokScript, OpenAIScript, openai_call

ONE_SECOND = b"\x00\x01" * 24_000


def test_key_and_model_come_from_the_environment() -> None:
    assert GrokVoiceProvider.required_env == (API_KEY_ENV,) == ("XAI_API_KEY",)
    provider = GrokVoiceProvider.from_env({API_KEY_ENV: "xai-k"})
    assert (provider.model, provider.voice) == ("grok-voice-think-fast-2.0", "eve")
    assert provider.url() == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0"
    assert provider.headers() == {"Authorization": "Bearer xai-k"}
    custom = GrokVoiceProvider.from_env({API_KEY_ENV: "k", "VAMOS_GROK_MODEL": "m", "VAMOS_GROK_VOICE": "v"})
    assert (custom.model, custom.voice) == ("m", "v")


def test_session_keeps_voice_instructions_and_vad_at_the_top_level() -> None:
    setup = REMI.setup()
    session = GrokVoiceProvider("k", "m", "eve").session_config(setup)
    assert session["voice"] == "eve"
    assert session["instructions"] == setup.instructions
    assert session["turn_detection"] == {"type": "server_vad"}, "xAI's own VAD defaults"
    pcm = {"type": "audio/pcm", "rate": 24_000}
    assert session["audio"] == {
        "input": {"format": pcm, "transcription": {"model": "grok-transcribe", "language_hint": "en"}},
        "output": {"format": pcm},
    }
    assert "tool_choice" not in session  # not part of Grok's documented session
    assert [t["name"] for t in session["tools"]] == [t.name for t in setup.tools]
    assert all(t["type"] == "function" for t in session["tools"])


async def _started_call(
    sock: FakeVendorSocket, provider, script
) -> tuple[FakeLine, CallRuntime, asyncio.Task]:
    line = FakeLine()
    call = runtime(REMI, line, ScriptedSession())
    task = asyncio.create_task(provider.run_call(call))
    await sock.wait_for(script.is_setup)
    return line, call, task


async def _barge_in(provider, script_cls) -> tuple[list[dict], int]:
    """Three seconds of reply at once, then the caller cuts in: the truncates sent, and the ms of
    the reply still accepted (written or queued) at the barge-in."""
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider.connect = sock.connector()
    line, call, task = await _started_call(sock, provider, script)
    script.callee_spoke()
    for _ in range(3):  # three seconds at once: two go to the line (the 1 s lead), one is queued
        sock.push(
            {
                "type": "response.output_audio.delta",
                "item_id": "itemA",
                "delta": base64.b64encode(ONE_SECOND).decode(),
            }
        )
    await asyncio.sleep(0.05)
    assert line.sent, "the reply reached the caller"
    sock.push({"type": "input_audio_buffer.speech_started", "item_id": "u2"})
    await asyncio.sleep(0.05)
    accepted = call.outbound.sent_ms("itemA")
    task.cancel()
    await call.outbound.close()  # as the server does when a call ends
    return [f for f in sock.sent if f.get("type") == "conversation.item.truncate"], accepted


async def test_barge_in_sends_no_truncate_to_grok_and_drops_nothing() -> None:
    truncates, accepted = await _barge_in(GrokVoiceProvider("k", "m", "eve"), GrokScript)
    assert truncates == []
    assert accepted == 3000, "Grok keeps the whole reply in its context: all of it is still to be heard"


async def test_barge_in_still_truncates_on_openai() -> None:
    (truncate,), accepted = await _barge_in(openai_call(FakeVendorSocket())["provider"], OpenAIScript)
    assert truncate["item_id"] == "itemA" and truncate["audio_end_ms"] == 2000, "what reached the caller"
    assert accepted == 2000, "the queued second was dropped"
