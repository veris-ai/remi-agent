"""Smoke: the real server running Remi, /health and /voice, a fake provider, a real Postgres.

The server runs Remi's screen pop (the connect reads, against the real
database) before the provider starts; the fake provider checks the account is
in its instructions and the gates are past the reads, tries to talk before
the callee (which the server must drop), answers after "Hello?", logs the call
and hangs up. The test is the caller on the /voice socket.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from typing import Self

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vamos_agents.remi import RemiAgent
from vamos_agents.remi.context import CallContext
from vamos_agents.remi.db import Database
from vamos_voice.framing import Framing
from vamos_voice.providers.base import CallRuntime, VoiceProvider
from vamos_voice.server import ConfigError, build_app_from_env, create_app

from .support import CONTEXT_JSON, app_dsn, changes, seed_full_world, snapshot

HELLO = b"\x01\x00" * 4800  # 200 ms of PCM16 at 24 kHz: stands in for "Hello?"

TOO_EARLY = b"\x07\x07" * 480
GREETING = b"\x09\x09" * 480


class FakeProvider(VoiceProvider):
    name = "fake"
    trace_adapter = None
    required_env = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls()

    async def run_call(self, call: CallRuntime) -> None:
        heard: list[bytes] = []
        first_heard = asyncio.Event()

        async def to_vendor(pcm: bytes) -> None:
            heard.append(pcm)
            first_heard.set()

        assert_screen_popped(call)
        call.inbound.bind(to_vendor)
        pump = asyncio.create_task(call.inbound.run())
        await call.outbound.send(TOO_EARLY, "early")  # a model that talks first: must never reach the caller
        await asyncio.wait_for(first_heard.wait(), 5)
        call.start.mark_callee_spoke()  # the vendor's VAD heard "Hello?"
        await call.outbound.send(GREETING, "greeting")
        await call.session.dispatch(
            "log_contact_attempt", {"answered_by": "none", "summary": "Smoke call: greeted and hung up."}
        )
        await call.session.dispatch("end_call", {})
        await call.hang_up()
        pump.cancel()


def assert_screen_popped(call: CallRuntime) -> None:
    """The screen pop ran before the provider: the account is in the instructions, the reads are done."""
    assert "# Account on screen" in call.setup.instructions
    assert call.session.state.missing_reads == []
    assert call.session.state.assessment is not None and call.session.state.assessment.proceed
    assert call.session.tool_log == [], "the screen pop is not a model tool call"


class ListeningProvider(FakeProvider):
    """Greets after the callee's Hello, then listens until the callee hangs up (no log, no end_call)."""

    async def run_call(self, call: CallRuntime) -> None:
        heard = asyncio.Event()

        async def to_vendor(pcm: bytes) -> None:
            heard.set()

        assert_screen_popped(call)
        call.inbound.bind(to_vendor)
        pump = asyncio.create_task(call.inbound.run())
        await asyncio.wait_for(heard.wait(), 5)
        call.start.mark_callee_spoke()
        await call.outbound.send(GREETING, "greeting")
        await pump  # the callee's {"type": "end"} ends it


@pytest.fixture
def app(pg_dsn: str):
    seed_full_world(pg_dsn)
    agent = RemiAgent(
        CallContext.from_json(CONTEXT_JSON), connect_db=lambda: Database.connect(app_dsn(pg_dsn))
    )
    return create_app(agent, FakeProvider())


@pytest.mark.postgres
def test_voice_call_end_to_end(app, pg_dsn: str) -> None:
    before = snapshot(pg_dsn)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "agent": "remi", "vendor": "fake"}
        with client.websocket_connect("/voice") as ws:
            ws.send_bytes(HELLO)
            first = ws.receive_bytes()
            assert first == GREETING, "the first audio the caller hears must come after their Hello"
            assert ws.receive()["type"] == "websocket.close"
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/voice"):
            pass
        assert refused.value.code == 1013  # one call per pod
    (change,) = changes(before, snapshot(pg_dsn))  # the World has earlier Remi calls: diff, not filter
    attempt = change["row"]
    assert (change["op"], change["table"], attempt["account_id"]) == (
        "insert",
        "contact_attempts",
        "TAS-420101",
    )
    assert attempt["disposition"] == "failed_verification" and attempt["duration_s"] >= 1


def test_startup_fails_loudly_on_missing_config() -> None:
    names = {"VAMOS_AGENT": "remi", "VAMOS_VENDOR": "openai_realtime"}
    world = {"DATABASE_URL": "postgresql://x"}
    with pytest.raises(ConfigError, match="DATABASE_URL") as missing:
        build_app_from_env(names)
    assert "STRIPE" not in str(missing.value)  # payments go through the World's database now
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        build_app_from_env({**names, "STRIPE_API_KEY": "sk_test_x"})
    with pytest.raises(ConfigError, match="VERIS_AGENT_CONTEXT"):
        build_app_from_env({**names, **world})
    base = {
        "VERIS_AGENT_CONTEXT": CONTEXT_JSON,
        "DATABASE_URL": "postgresql://x",
        "VAMOS_AGENT": "remi",
    }
    with pytest.raises(ConfigError, match="VAMOS_VENDOR"):
        build_app_from_env(base)
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_app_from_env({**base, "VAMOS_VENDOR": "openai_realtime"})


def _audio(pcm: bytes) -> str:
    return json.dumps({"type": "audio", "audio": base64.b64encode(pcm).decode()})


@pytest.mark.postgres
def test_json_framing_agent_hang_up_sends_end(pg_dsn: str) -> None:
    """The bench's json framing: the agent's end_call reaches the actor as {"type": "end"}, then a 1000 close,
    so the actor records a normal end (client_end) rather than a dropped socket."""
    seed_full_world(pg_dsn)
    agent = RemiAgent(
        CallContext.from_json(CONTEXT_JSON), connect_db=lambda: Database.connect(app_dsn(pg_dsn))
    )
    app = create_app(agent, FakeProvider(), framing=Framing.JSON)
    with TestClient(app) as client, client.websocket_connect("/voice") as ws:
        ws.send_text(_audio(HELLO))
        assert ws.receive_text() == _audio(GREETING)
        assert json.loads(ws.receive_text()) == {"type": "end"}
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000


@pytest.mark.postgres
def test_json_framing_callee_end_still_logs_the_contact_attempt(pg_dsn: str) -> None:
    """The callee hangs up in-band before Remi logs the call: finalize() writes the fallback row, as on a drop."""
    seed_full_world(pg_dsn)
    agent = RemiAgent(
        CallContext.from_json(CONTEXT_JSON), connect_db=lambda: Database.connect(app_dsn(pg_dsn))
    )
    before = snapshot(pg_dsn)
    with (
        TestClient(create_app(agent, ListeningProvider(), framing=Framing.JSON)) as client,
        client.websocket_connect("/voice") as ws,
    ):
        ws.send_text(_audio(HELLO))
        assert ws.receive_text() == _audio(GREETING)
        ws.send_text(json.dumps({"type": "end"}))
        assert ws.receive()["type"] == "websocket.close", "no end is echoed back to the callee"
    (change,) = changes(before, snapshot(pg_dsn))
    assert (change["op"], change["table"]) == ("insert", "contact_attempts")
    assert change["row"]["summary"].endswith("The call ended before Remi logged it.")
