"""Realtime-protocol call flow, against a scripted vendor.

Turn taking is the vendor's own (server VAD replies and interrupts); these tests
pin what the provider still does itself:

- Call start sends the session and nothing that makes the model act: no priming
  response (REMI-082: a priming response once spoke "Hello... I'm here" aloud),
  so the first response is the vendor's own answer to the callee, in audio.
- The provider's own requests (tool results) never race a running response:
  they wait for it, and one refused with ``conversation_already_has_active_response``
  is sent again when the running one ends.
- No custom turn gating: the provider never answers a caller turn itself and
  never cancels a response. The model's context holds exactly what the caller
  heard (everything written to the line): on speech, OpenAI's playback stops
  (queued agent audio and the rest of the interrupted item are dropped) and its
  context is truncated to what was written; Grok, which cannot truncate, keeps
  its whole reply in context, so nothing of it is dropped.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
from typing import Any

import pytest

from .fakes import HELLO, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime
from .test_parity import grok_call, openai_call


class Vendor:
    """A scripted Realtime server (GA event names) on a ``FakeVendorSocket``."""

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n, self.input_ms = sock, 0, 0

    def created(self) -> str:
        self.n += 1
        rid = f"r{self.n}"
        self.sock.push({"type": "response.created", "response": {"id": rid}})
        return rid

    def done(self, rid: str, status: str = "completed") -> None:
        self.sock.push({"type": "response.done", "response": {"id": rid, "status": status}})

    def tool_calls(self, rid: str, *names: str) -> None:
        for i, name in enumerate(names):
            self.sock.push(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": rid,
                    "call_id": f"call_{rid}_{i}",
                    "name": name,
                    "arguments": "{}",
                }
            )

    def audio(self, rid: str, ms: int = 1000, pieces: int = 1) -> None:
        """``pieces`` deltas of ``ms`` each, at once: the vendor streams faster than speech plays."""
        delta = base64.b64encode(b"\x00\x10" * 24 * ms).decode()
        event = {
            "type": "response.output_audio.delta",
            "response_id": rid,
            "item_id": f"i{rid}",
            "delta": delta,
        }
        for _ in range(pieces):
            self.sock.push(event)

    def audio_done(self, rid: str) -> None:
        self.sock.push({"type": "response.output_audio.done", "response_id": rid, "item_id": f"i{rid}"})

    def turn(self, start_ms: int, end_ms: int, *, commit: bool = True) -> None:
        """Server VAD on the caller's audio between ``start_ms`` and ``end_ms`` (input timeline)."""
        self.sock.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": start_ms})
        if commit:
            self.sock.push({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": end_ms})
            self.sock.push({"type": "input_audio_buffer.committed", "item_id": f"u{start_ms}"})


def sent(sock: FakeVendorSocket, kind: str) -> list[dict[str, Any]]:
    return [f for f in sock.sent if f.get("type") == kind]


def is_reply(frame: dict[str, Any]) -> bool:
    """A response.create the provider asked for."""
    return frame.get("type") == "response.create"


async def settle() -> None:
    await asyncio.sleep(0.05)


async def start(build=openai_call) -> tuple[FakeVendorSocket, Vendor, FakeLine, Any, asyncio.Task]:
    sock = FakeVendorSocket()
    provider = build(sock)["provider"]
    line = FakeLine()
    call = runtime(REMI, line, ScriptedSession())
    task = asyncio.create_task(provider.run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "session.update")
    return sock, Vendor(sock), line, call, task


async def answered(line: FakeLine) -> None:
    """The callee says "Hello?"; it goes straight to the vendor."""
    await line.incoming.put(HELLO)
    await settle()


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_call_start_sends_only_the_session_and_the_callee_is_answered_in_audio() -> None:
    sock, vendor, line, _call, task = await start()
    await answered(line)
    assert [f["type"] for f in sock.sent] == ["session.update", "input_audio_buffer.append"]
    assert sock.sent[0]["session"]["output_modalities"] == ["audio"]

    vendor.turn(0, 200)  # "Hello?": the vendor's VAD answers it
    await settle()
    assert not any(is_reply(f) for f in sock.sent), "the vendor replies to caller turns, not the provider"
    assert len(sent(sock, "session.update")) == 1, "the session is never rewritten"

    rid = vendor.created()
    vendor.audio(rid)
    await settle()
    assert line.sent, "the agent's reply reached the callee"
    await stop(task)


@pytest.mark.parametrize("build", [openai_call, grok_call])
async def test_turn_taking_is_the_vendors_own(build) -> None:
    sock, vendor, line, call, task = await start(build)
    session = sent(sock, "session.update")[0]["session"]
    vad = (
        session["turn_detection"]
        if "turn_detection" in session
        else session["audio"]["input"]["turn_detection"]
    )
    assert vad["type"] == "server_vad"
    assert vad.get("create_response", True) and vad.get("interrupt_response", True)

    await answered(line)
    before = len(sock.sent)
    vendor.turn(0, 200)
    await settle()
    assert call.start.callee_spoke
    assert not any(f.get("type") in ("response.create", "response.cancel") for f in sock.sent[before:])
    await stop(task)


def line_ms(line: FakeLine) -> float:
    return sum(map(len, line.sent)) / 48


async def test_caller_speech_over_the_agent_only_truncates() -> None:
    sock, vendor, line, call, task = await start()
    await answered(line)
    vendor.turn(0, 200)
    reply = vendor.created()
    vendor.audio(reply, ms=100, pieces=50)  # 5 s, streamed at once
    await settle()

    vendor.turn(200, 400, commit=False)  # the vendor's VAD hears speech and stops its response
    await settle()
    assert sent(sock, "response.cancel") == [], "the vendor interrupts itself"
    (truncate,) = sent(sock, "conversation.item.truncate")
    assert truncate["item_id"] == f"i{reply}" and truncate["audio_end_ms"] < 5000
    assert truncate["audio_end_ms"] == call.outbound.heard_ms(f"i{reply}") == line_ms(line)
    await stop(task)


async def test_barge_in_drops_queued_audio_and_truncates_to_what_the_caller_hears() -> None:
    """The caller hears every byte written to the line (the bench's caller never drops queued agent
    audio), so the context is cut exactly there, and nothing more of the reply is written."""
    sock, vendor, line, call, task = await start()
    await answered(line)
    vendor.turn(0, 200)  # the callee spoke: agent audio may leave
    reply = vendor.created()
    vendor.audio(reply, ms=100, pieces=30)  # 3 s, streamed at once
    await settle()
    assert 1000 <= line_ms(line) <= 1200, "at most the lead (1 s) and one piece ahead of real time"
    vendor.audio(reply, ms=100, pieces=1)  # still arriving when the caller cuts in
    vendor.turn(200, 400, commit=False)
    vendor.audio(reply, ms=100, pieces=5)  # what the vendor sends before it has stopped
    vendor.audio_done(reply)
    vendor.done(reply, status="cancelled")
    await asyncio.sleep(0.3)  # the pump would have written three more pieces by now
    (truncate,) = sent(sock, "conversation.item.truncate")
    written = line_ms(line)
    assert truncate == {
        "type": "conversation.item.truncate",
        "item_id": f"i{reply}",
        "content_index": 0,
        "audio_end_ms": written,
    }, "cut at the barge-in, to exactly what reached the caller; nothing reached it since"
    assert call.outbound.sent_ms(f"i{reply}") == call.outbound.heard_ms(f"i{reply}") == written
    assert 1000 <= written < 1500
    await stop(task)


async def test_barge_in_while_the_reply_is_still_streaming_truncates_though_nothing_was_queued() -> None:
    sock, vendor, line, call, task = await start()
    await answered(line)
    vendor.turn(0, 200)  # the callee spoke: agent audio may leave
    reply = vendor.created()
    vendor.audio(reply, ms=500)  # written at once: the line is idle
    await settle()
    vendor.turn(200, 400, commit=False)
    await settle()
    (truncate,) = sent(sock, "conversation.item.truncate")
    assert truncate["audio_end_ms"] == 500, "the rest of the reply will not be heard"
    vendor.audio(reply, ms=300)  # the vendor had not stopped yet: dropped
    await settle()
    assert line_ms(line) == 500 and call.outbound.heard_ms(f"i{reply}") == 500
    await stop(task)


@pytest.mark.parametrize("audio_done", [True, False])
async def test_barge_in_after_a_reply_written_in_full_truncates_nothing(audio_done: bool) -> None:
    """The caller hears all of it: cutting the context would make the model forget words it said."""
    sock, vendor, line, _call, task = await start()
    await answered(line)
    vendor.turn(0, 200)  # the callee spoke: agent audio may leave
    reply = vendor.created()
    vendor.audio(reply, ms=500)
    if audio_done:
        vendor.audio_done(reply)
    vendor.done(reply)
    await settle()
    vendor.turn(200, 400, commit=False)
    await settle()
    assert sent(sock, "conversation.item.truncate") == []
    assert line_ms(line) == 500
    await stop(task)


async def test_grok_barge_in_drops_nothing_and_sends_no_truncate() -> None:
    """Grok keeps its whole reply in its context and cannot be told otherwise, so the caller hears
    all of it: queued audio and what Grok still sends after the caller cut in both play."""
    sock, vendor, line, call, task = await start(grok_call)
    await answered(line)
    vendor.turn(0, 200)  # the callee spoke: agent audio may leave
    reply = vendor.created()
    vendor.audio(reply, ms=100, pieces=30)
    await settle()
    vendor.turn(200, 400, commit=False)
    vendor.audio(reply, ms=100, pieces=5)
    await asyncio.sleep(0.3)
    assert sent(sock, "conversation.item.truncate") == [], "xAI documents truncate as unsupported"
    assert call.outbound.sent_ms(f"i{reply}") == 3500, "nothing was dropped"
    written = line_ms(line)
    assert call.outbound.heard_ms(f"i{reply}") == written < 3500, "still playing at speaking pace"
    await stop(task)


async def test_a_request_refused_for_an_active_response_is_sent_when_it_ends() -> None:
    sock, vendor, line, _call, task = await start()
    await answered(line)
    rid = vendor.created()
    vendor.tool_calls(rid, "get_account")
    vendor.done(rid)
    await sock.wait_for(is_reply)  # the tool-results follow-up
    sock.push(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "code": "conversation_already_has_active_response"},
        }
    )
    other = vendor.created()  # the response that was already running
    await settle()
    assert sum(is_reply(f) for f in sock.sent) == 1
    vendor.done(other)
    await settle()
    assert sum(is_reply(f) for f in sock.sent) == 2, "the refused request is asked for again"
    await stop(task)


def test_openai_realtime_pins_the_callers_transcription_to_english() -> None:
    """The VAmoS benches are English-only: the language is configuration where the vendor takes one."""
    from vamos_voice.providers.openai_realtime import OpenAIRealtimeProvider

    provider = OpenAIRealtimeProvider.from_env({"OPENAI_API_KEY": "sk-test"})
    transcription = provider.session_config(REMI.setup())["audio"]["input"]["transcription"]
    assert transcription["language"] == "en"


def test_grok_voice_and_gemini_live_pin_the_callers_transcription_to_english() -> None:
    from vamos_voice.providers.gemini_live import GeminiLiveProvider
    from vamos_voice.providers.grok_voice import API_KEY_ENV, GrokVoiceProvider

    grok = GrokVoiceProvider.from_env({API_KEY_ENV: "k"}).session_config(REMI.setup())
    assert grok["audio"]["input"]["transcription"]["language_hint"] == "en"
    for model in ("gemini-2.5-flash-native-audio-preview-12-2025", "gemini-3.8-live"):
        gemini = GeminiLiveProvider("k", model, "Puck").setup_message(REMI.setup())["setup"]
        assert gemini["inputAudioTranscription"] == {"languageCodes": ["en"]}, model
        assert "languageCode" not in gemini["generationConfig"]["speechConfig"], "native audio takes none"


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_speech_services_are_pinned_to_english() -> None:
    from pipecat.transcriptions.language import Language

    from vamos_voice.providers.pipecat.services import STT_FACTORIES, TTS_FACTORIES, ServiceConfig

    config = ServiceConfig.from_env(
        {
            "OPENAI_API_KEY": "sk-test",
            "DEEPGRAM_API_KEY": "dg-test",
            "CARTESIA_API_KEY": "ca-test",
            "VAMOS_CARTESIA_VOICE_ID": "voice",
        }
    )
    for factory in (STT_FACTORIES["deepgram"], STT_FACTORIES["openai"], TTS_FACTORIES["cartesia"]):
        assert factory(config)._settings.language == Language.EN, factory.__name__
