"""The two ``voice_ws`` framings, on their own and through the real server.

``json`` must match the bench actor exactly (veris-sandbox
``actor-service/app/actor/channels/voice_ws.py``): base64 audio envelopes both
ways, ``{"type": "end"}`` sent before the agent's close (the actor then records
``client_end``, a normal end, instead of ``voice_ws_disconnect``), and a caller's
``end`` ends the call gracefully. ``binary`` must stay what it was: raw bytes,
no text frame, the close handshake as the hang-up.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from collections.abc import Mapping
from typing import Self

import pytest
from fastapi.testclient import TestClient

from vamos_voice.agent import ConfigError
from vamos_voice.context import AgentContext, CallClock
from vamos_voice.framing import CALLER_END, FRAMING_ENV, CallerMessage, Framing, framing_from_env
from vamos_voice.providers.base import CallRuntime, VoiceProvider
from vamos_voice.server import create_app

from .fake_inbound import FakeInboundAgent, IvyCall
from .fakes import HELLO

GREETING = b"\x09\x09" * 480


def envelope(pcm: bytes) -> str:
    return json.dumps({"type": "audio", "audio": base64.b64encode(pcm).decode()})


# --- the codec --------------------------------------------------------------------------------


def test_json_audio_round_trips_as_base64_envelopes() -> None:
    encoded = Framing.JSON.encode_audio(GREETING)
    assert isinstance(encoded, str), "json audio goes out as a text frame"
    assert json.loads(encoded) == {"type": "audio", "audio": base64.b64encode(GREETING).decode()}
    assert Framing.JSON.decode(encoded) == CallerMessage(audio=GREETING)
    assert Framing.JSON.decode(encoded.encode()) == CallerMessage(audio=GREETING), "bytes carrying JSON"


def test_json_end_and_what_is_ignored() -> None:
    assert Framing.JSON.end_message() == '{"type": "end"}'
    assert Framing.JSON.decode('{"type": "end"}') is CALLER_END
    for ignored in ('{"type": "mark", "name": "x"}', "not json", "[1]", '{"type": "audio", "audio": ""}'):
        assert Framing.JSON.decode(ignored) is None, ignored
    assert Framing.JSON.decode('{"type": "audio", "audio": "!!not base64!!"}') is None


def test_binary_framing_is_raw_bytes_and_the_close() -> None:
    assert Framing.BINARY.encode_audio(GREETING) is GREETING
    assert Framing.BINARY.end_message() is None
    assert Framing.BINARY.decode(HELLO) == CallerMessage(audio=HELLO)
    assert Framing.BINARY.decode(b"") is None
    assert Framing.BINARY.decode('{"type": "end"}') is None, "binary has no in-band hang-up"


def test_framing_from_env() -> None:
    assert framing_from_env({}) is Framing.BINARY
    assert framing_from_env({FRAMING_ENV: ""}) is Framing.BINARY
    assert framing_from_env({FRAMING_ENV: "json"}) is Framing.JSON
    with pytest.raises(ConfigError, match=FRAMING_ENV):
        framing_from_env({FRAMING_ENV: "JSON-ish"})


# --- through the real server --------------------------------------------------------------------


class RecordingAgent(FakeInboundAgent):
    """The test-only inbound agent, keeping its call state so a test can see it finalized."""

    calls: list[IvyCall]

    def open_call(self, clock: CallClock) -> IvyCall:
        state = super().open_call(clock)
        self.calls.append(state)
        return state


def recording_agent() -> RecordingAgent:
    agent = RecordingAgent.from_env({}, AgentContext.from_json(None))
    agent.calls = []
    return agent


class HangUpProvider(VoiceProvider):
    """Greets, waits to hear the caller, then the agent ends the call (``end_call``)."""

    name = "fake"
    trace_adapter = None
    required_env = ()

    def __init__(self) -> None:
        self.heard: list[bytes] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls()

    async def run_call(self, call: CallRuntime) -> None:
        heard = asyncio.Event()

        async def to_vendor(pcm: bytes) -> None:
            self.heard.append(pcm)
            heard.set()

        call.inbound.bind(to_vendor)
        pump = asyncio.create_task(call.inbound.run())
        call.start.mark_primed()
        await call.inbound.release()
        assert await call.outbound.send(GREETING, "greeting")
        await asyncio.wait_for(heard.wait(), 5)
        await call.session.dispatch("end_call", {})
        await call.hang_up()
        pump.cancel()


class ListenProvider(HangUpProvider):
    """Greets and listens until the caller hangs up; the agent never ends the call."""

    async def run_call(self, call: CallRuntime) -> None:
        async def to_vendor(pcm: bytes) -> None:
            self.heard.append(pcm)

        call.inbound.bind(to_vendor)
        call.start.mark_primed()
        await call.inbound.release()
        assert await call.outbound.send(GREETING, "greeting")
        await call.inbound.run()  # returns when the caller hangs up


def test_json_agent_hang_up_sends_end_then_closes_normally() -> None:
    agent, provider = recording_agent(), HangUpProvider()
    with (
        TestClient(create_app(agent, provider, framing=Framing.JSON)) as client,
        client.websocket_connect("/voice") as ws,
    ):
        assert json.loads(ws.receive_text()) == json.loads(envelope(GREETING))
        ws.send_text(envelope(HELLO))
        assert json.loads(ws.receive_text()) == {"type": "end"}, "end comes before the close"
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000
    assert provider.heard == [HELLO], "the caller's base64 audio arrives decoded"
    (call,) = agent.calls
    assert call.ended and call.finalized


def test_json_caller_end_finalizes_the_call_without_echoing_end() -> None:
    agent, provider = recording_agent(), ListenProvider()
    with (
        TestClient(create_app(agent, provider, framing=Framing.JSON)) as client,
        client.websocket_connect("/voice") as ws,
    ):
        assert json.loads(ws.receive_text()) == json.loads(envelope(GREETING))
        ws.send_text(envelope(HELLO))
        ws.send_text('{"type": "transcript", "text": "ignored"}')  # unknown types are ignored
        ws.send_text('{"type": "end"}')
        message = ws.receive()
        assert message["type"] == "websocket.close", f"nothing after the caller's end: {message}"
    assert provider.heard == [HELLO]
    (call,) = agent.calls
    assert call.finalized and not call.ended, "the caller hung up; the agent did not"


class FailingProvider(HangUpProvider):
    """The vendor fails before the call starts (e.g. priming could not reach it)."""

    async def run_call(self, call: CallRuntime) -> None:
        raise RuntimeError("vendor unreachable")


def test_json_server_side_failure_still_hangs_up_cleanly() -> None:
    agent = recording_agent()
    with (
        TestClient(create_app(agent, FailingProvider(), framing=Framing.JSON)) as client,
        client.websocket_connect("/voice") as ws,
    ):
        assert json.loads(ws.receive_text()) == {"type": "end"}
        assert ws.receive()["code"] == 1000
    assert agent.calls[0].finalized


def test_binary_framing_is_unchanged() -> None:
    """Golden: raw bytes both ways, no text frame, the hang-up is the close (1000) itself."""
    agent, provider = recording_agent(), HangUpProvider()
    with TestClient(create_app(agent, provider)) as client, client.websocket_connect("/voice") as ws:
        assert ws.receive() == {"type": "websocket.send", "bytes": GREETING}
        ws.send_bytes(HELLO)
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000
    assert provider.heard == [HELLO]
    assert agent.calls[0].finalized


# --- Pipecat: its transport owns the socket, the serializer applies the framing -----------------


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_serializer_json_framing() -> None:
    from pipecat.frames.frames import CancelFrame, CancelWorkerFrame, EndFrame, OutputAudioRawFrame

    from vamos_voice.providers.pipecat.serializer import LineSerializer

    agent_ends = LineSerializer(Framing.JSON)
    out = await agent_ends.serialize(OutputAudioRawFrame(audio=GREETING, sample_rate=24_000, num_channels=1))
    assert out == envelope(GREETING)
    inbound = await agent_ends.deserialize(envelope(HELLO))
    assert inbound.audio == HELLO and inbound.sample_rate == 24_000
    assert await agent_ends.serialize(EndFrame()) == '{"type": "end"}', "the agent hangs up in-band"

    caller_ends = LineSerializer(Framing.JSON)
    assert isinstance(await caller_ends.deserialize('{"type": "end"}'), CancelWorkerFrame)
    assert await caller_ends.serialize(CancelFrame()) is None, "no end back to a caller who hung up"
    assert await caller_ends.deserialize('{"type": "mark"}') is None


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_serializer_binary_framing_is_unchanged() -> None:
    from pipecat.frames.frames import EndFrame, OutputAudioRawFrame

    from vamos_voice.providers.pipecat.serializer import LineSerializer

    binary = LineSerializer(Framing.BINARY)
    out = await binary.serialize(OutputAudioRawFrame(audio=GREETING, sample_rate=24_000, num_channels=1))
    assert out == GREETING
    assert (await binary.deserialize(HELLO)).audio == HELLO
    assert await binary.serialize(EndFrame()) is None, "binary hangs up with the close alone"
    assert await binary.deserialize('{"type": "end"}') is None
