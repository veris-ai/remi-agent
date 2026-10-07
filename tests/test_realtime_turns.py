"""Realtime-protocol call flow after priming: each failure seen on the bench, replayed offline.

Smoke trial ``trial_tf9ud89g67fpyh8l2tb37`` (Remi on gpt-realtime-2.1) lost three
of ten calls right after the call start. These tests script a fake vendor
through each failure and prove the agent's reply reaches the caller after the
callee's first utterance:

- REMI-027 / REMI-090: a TV behind the callee. Server VAD made every broadcast
  phrase a caller turn; with the vendor answering and interrupting on its own,
  each reply was cancelled within a second (silence, or fragments like "Irish",
  "This is", "Hi").
- REMI-082: the priming response spoke for five seconds instead of calling the
  reads; the deadline released the voicemail greeting while it was still in
  flight, and server VAD cancelled it.
- Responses lost to a race (``conversation_already_has_active_response``) or
  never started: the liveness guard asks for one.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import replace
from typing import Any

import pytest

from vamos_voice.providers.base import TurnTaking

from .fakes import BACKGROUND, HELLO, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime, tone
from .test_parity import grok_call, openai_call

READS = REMI.policy.connect_reads


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

    def audio(self, rid: str, ms: int = 1000) -> None:
        delta = base64.b64encode(b"\x00\x10" * 24 * ms).decode()
        self.sock.push(
            {"type": "response.output_audio.delta", "response_id": rid, "item_id": f"i{rid}", "delta": delta}
        )

    def turn(self, start_ms: int, end_ms: int, *, commit: bool = True) -> None:
        """Server VAD on the caller's audio between ``start_ms`` and ``end_ms`` (input timeline)."""
        self.sock.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": start_ms})
        if commit:
            self.sock.push({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": end_ms})
            self.sock.push({"type": "input_audio_buffer.committed", "item_id": f"u{start_ms}"})


def sent(sock: FakeVendorSocket, kind: str) -> list[dict[str, Any]]:
    return [f for f in sock.sent if f.get("type") == kind]


def is_reply(frame: dict[str, Any]) -> bool:
    """A response.create for the conversation (not a priming one)."""
    return frame.get("type") == "response.create" and "tool_choice" not in frame.get("response", {})


async def settle() -> None:
    await asyncio.sleep(0.05)


async def start(
    build=openai_call, *, priming_timeout_s: float = 6.0, reply_timeout_s: float = 4.0
) -> tuple[FakeVendorSocket, Vendor, FakeLine, Any, asyncio.Task]:
    sock = FakeVendorSocket()
    provider = build(sock)["provider"]
    line = FakeLine()
    call = runtime(REMI, line, ScriptedSession(READS), priming_timeout_s=priming_timeout_s)
    call.setup = replace(call.setup, turn_taking=TurnTaking(reply_timeout_s=reply_timeout_s))
    task = asyncio.create_task(provider.run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "response.create")  # the priming request
    return sock, Vendor(sock), line, call, task


async def primed(vendor: Vendor, line: FakeLine) -> None:
    """The callee says "Hello?" while the reads run; the reads finish and release it."""
    await line.incoming.put(HELLO)
    rid = vendor.created()
    vendor.tool_calls(rid, *READS)
    vendor.done(rid)
    await settle()


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_priming_is_text_only_and_the_callee_is_answered_in_audio() -> None:
    sock, vendor, line, _call, task = await start()
    (prime,) = sent(sock, "response.create")
    setup = REMI.setup()
    assert prime["response"] == {
        "tool_choice": "required",
        "output_modalities": ["text"],  # it cannot talk for seconds before running the reads
        "instructions": f"{setup.instructions}\n\n{REMI.policy.prime_instruction}",
    }

    await primed(vendor, line)
    vendor.turn(0, 200)  # "Hello?"
    reply = await sock.wait_for(is_reply)
    assert reply == {"type": "response.create"}, "the reply inherits the session: audio, tool_choice auto"
    assert len(sent(sock, "session.update")) == 1, "priming never rewrites the session"
    assert sent(sock, "session.update")[0]["session"]["output_modalities"] == ["audio"]

    rid = vendor.created()
    vendor.audio(rid)
    await settle()
    assert line.sent, "the agent's reply reached the callee"
    await stop(task)


async def test_the_session_hands_turns_to_the_provider() -> None:
    sock, *_, task = await start()
    vad = sent(sock, "session.update")[0]["session"]["audio"]["input"]["turn_detection"]
    assert vad["create_response"] is False and vad["interrupt_response"] is False
    await stop(task)


async def test_deadline_mid_priming_keeps_the_priming_response_and_answers_after_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REMI-082: the deadline released the greeting while the priming response was still running."""
    sock, vendor, line, call, task = await start(priming_timeout_s=0.2)
    greeting = tone(2000, -15)  # "Hi, you've reached Marcus Sokolov..."
    await line.incoming.put(greeting)
    prime = vendor.created()  # the priming response is running, no read yet
    await sock.wait_for(lambda f: f.get("type") == "input_audio_buffer.append", within=2)
    assert "not done after 0.2s" in caplog.text

    vendor.turn(0, 2000)  # the greeting, heard while priming is still in flight
    await settle()
    assert sent(sock, "response.cancel") == [], "the priming response is not cancelled"
    assert not any(is_reply(f) for f in sock.sent), "no reply is requested over a running response"

    vendor.tool_calls(prime, *READS)  # its reads arrive late, then it finishes
    vendor.done(prime)
    await settle()
    assert [c for c, _ in call.session.calls] == list(READS)
    assert sum(is_reply(f) for f in sock.sent) == 1, "one reply covers the read results and the greeting"

    rid = vendor.created()
    vendor.audio(rid)
    await settle()
    assert line.sent, "the agent answers the greeting"
    await stop(task)


async def test_a_priming_response_that_never_runs_the_reads_still_leads_to_a_reply() -> None:
    sock, vendor, line, _call, task = await start(priming_timeout_s=0.2)
    await line.incoming.put(HELLO)
    prime = vendor.created()
    await sock.wait_for(lambda f: f.get("type") == "input_audio_buffer.append", within=2)
    vendor.turn(0, 200)
    await settle()
    vendor.done(prime, "cancelled")  # it ends with no read at all
    await sock.wait_for(is_reply)
    rid = vendor.created()
    vendor.audio(rid)
    await settle()
    assert line.sent
    await stop(task)


async def test_background_speech_neither_interrupts_nor_is_answered(caplog: pytest.LogCaptureFixture) -> None:
    """REMI-027 / REMI-090: a television at -42 dBFS behind a caller who speaks at -12."""
    caplog.set_level(logging.INFO)
    sock, vendor, line, _call, task = await start()
    await primed(vendor, line)
    vendor.turn(0, 200)
    await sock.wait_for(is_reply)
    reply = vendor.created()
    vendor.audio(reply, ms=3000)  # "Hi, I'm Remi with Tallmere Account Services..."
    await settle()
    heard = len(line.sent)
    assert heard

    for i in range(3):  # three broadcast phrases while the agent talks and after it stops
        await line.incoming.put(BACKGROUND)
        await settle()
        vendor.turn(200 + i * 1000, 1200 + i * 1000)
        await settle()
    assert sent(sock, "response.cancel") == [] and sent(sock, "conversation.item.truncate") == []
    assert sum(is_reply(f) for f in sock.sent) == 1, "a background-only turn gets no reply"
    assert "only background audio" in caplog.text

    vendor.audio(reply)  # the reply keeps playing
    await settle()
    assert len(line.sent) > heard
    vendor.done(reply)

    await line.incoming.put(HELLO)  # the caller's own voice: answered
    await settle()
    vendor.turn(3200, 3400)
    await settle()
    assert sum(is_reply(f) for f in sock.sent) == 2, "the caller's own voice is answered"
    await stop(task)


async def test_the_callers_own_voice_interrupts_the_agent() -> None:
    sock, vendor, line, _call, task = await start()
    await primed(vendor, line)
    vendor.turn(0, 200)
    await sock.wait_for(is_reply)
    reply = vendor.created()
    vendor.audio(reply, ms=5000)
    await settle()

    await line.incoming.put(HELLO)  # talking over the agent
    await settle()
    vendor.turn(200, 400, commit=False)
    await sock.wait_for(lambda f: f.get("type") == "response.cancel")
    (truncate,) = sent(sock, "conversation.item.truncate")
    assert truncate["item_id"] == f"i{reply}" and truncate["audio_end_ms"] < 5000

    sock.push({"type": "input_audio_buffer.committed", "item_id": "u2"})
    await settle()
    assert sum(is_reply(f) for f in sock.sent) == 1, "the new reply waits for the cancelled one to end"
    vendor.done(reply, "cancelled")
    await settle()
    assert sum(is_reply(f) for f in sock.sent) == 2, "a cancelled response is followed by a reply"
    await stop(task)


async def test_a_reply_lost_to_an_active_response_is_sent_when_it_ends() -> None:
    sock, vendor, line, _call, task = await start()
    await primed(vendor, line)
    vendor.turn(0, 200)
    await sock.wait_for(is_reply)
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
    assert sum(is_reply(f) for f in sock.sent) == 2, "the refused reply is asked for again"
    await stop(task)


async def test_liveness_requests_a_reply_when_none_starts(caplog: pytest.LogCaptureFixture) -> None:
    sock, vendor, line, _call, task = await start(reply_timeout_s=0.2)
    await primed(vendor, line)
    vendor.turn(0, 200)
    await sock.wait_for(is_reply)  # the vendor never starts it
    await asyncio.sleep(0.35)
    assert sum(is_reply(f) for f in sock.sent) == 2
    assert "liveness" in caplog.text and "requesting one" in caplog.text

    caplog.clear()
    vendor.created()  # a reply starts: the guard stands down
    await asyncio.sleep(0.35)
    assert sum(is_reply(f) for f in sock.sent) == 2
    assert "liveness" not in caplog.text
    await stop(task)


async def test_liveness_on_grok_whose_vad_answers_on_its_own(caplog: pytest.LogCaptureFixture) -> None:
    sock = FakeVendorSocket()
    provider = grok_call(sock)["provider"]
    line = FakeLine()
    call = runtime(REMI, line, ScriptedSession(READS))
    call.setup = replace(call.setup, turn_taking=TurnTaking(reply_timeout_s=0.2))
    task = asyncio.create_task(provider.run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "response.create")
    vendor = Vendor(sock)
    await primed(vendor, line)
    before = sum(f == {"type": "response.create"} for f in sock.sent)

    sock.push({"type": "input_audio_buffer.committed", "item_id": "u1"})  # Grok creates no response
    await asyncio.sleep(0.35)
    assert sum(f == {"type": "response.create"} for f in sock.sent) == before + 1
    assert "liveness" in caplog.text
    assert call.start.callee_spoke
    await stop(task)
