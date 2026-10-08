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
  never cancels a response; on speech it only truncates what was not heard.
"""

from __future__ import annotations

import asyncio
import base64
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


async def test_caller_speech_over_the_agent_only_truncates() -> None:
    sock, vendor, line, _call, task = await start()
    await answered(line)
    vendor.turn(0, 200)
    reply = vendor.created()
    vendor.audio(reply, ms=5000)
    await settle()

    vendor.turn(200, 400, commit=False)  # the vendor's VAD hears speech and stops its response
    await settle()
    assert sent(sock, "response.cancel") == [], "the vendor interrupts itself"
    (truncate,) = sent(sock, "conversation.item.truncate")
    assert truncate["item_id"] == f"i{reply}" and truncate["audio_end_ms"] < 5000
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
