"""Smoke: the real server running Remi, /health and /voice, a fake provider, a real Postgres.

The fake provider plays a model: it primes the connect reads, tries to talk
before the callee (which the server must drop), answers after "Hello?",
logs the call and hangs up. The test is the caller on the /voice socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Self

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vamos_agents.remi import RemiAgent
from vamos_agents.remi.context import CallContext
from vamos_agents.remi.db import Database
from vamos_voice.providers.base import CallRuntime, VoiceProvider
from vamos_voice.server import ConfigError, build_app_from_env, create_app

from .support import CONTEXT_JSON, FakePayments, rows, seed_world

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

        call.inbound.bind(to_vendor)
        pump = asyncio.create_task(call.inbound.run())
        for read in ("get_account", "get_dialed_phone", "get_contact_history"):
            assert "error" not in await call.session.dispatch(read, {})
        assert not heard, "caller audio must be held while priming"
        await call.outbound.send(TOO_EARLY, "early")  # a model that talks first: must never reach the caller
        call.start.mark_primed()
        await call.inbound.release()
        await asyncio.wait_for(first_heard.wait(), 5)
        call.start.mark_callee_spoke()  # the vendor's VAD heard "Hello?"
        await call.outbound.send(GREETING, "greeting")
        await call.session.dispatch(
            "log_contact_attempt", {"answered_by": "none", "summary": "Smoke call: greeted and hung up."}
        )
        await call.session.dispatch("end_call", {})
        await call.hang_up()
        pump.cancel()


@pytest.fixture
def app(pg_dsn: str):
    seed_world(pg_dsn)
    agent = RemiAgent(
        CallContext.from_json(CONTEXT_JSON),
        connect_db=lambda: Database.connect(pg_dsn),
        payments=FakePayments(),
    )
    return create_app(agent, FakeProvider())


@pytest.mark.postgres
def test_voice_call_end_to_end(app, pg_dsn: str) -> None:
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
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert attempt["disposition"] == "failed_verification" and attempt["duration_s"] >= 1


def test_startup_fails_loudly_on_missing_config() -> None:
    names = {"VAMOS_AGENT": "remi", "VAMOS_VENDOR": "openai_realtime"}
    world = {"DATABASE_URL": "postgresql://x", "STRIPE_API_KEY": "sk_test_x"}
    with pytest.raises(ConfigError, match="DATABASE_URL, STRIPE_API_KEY"):
        build_app_from_env(names)
    with pytest.raises(ConfigError, match="VERIS_AGENT_CONTEXT"):
        build_app_from_env({**names, **world})
    base = {
        "VERIS_AGENT_CONTEXT": CONTEXT_JSON,
        "DATABASE_URL": "postgresql://x",
        "STRIPE_API_KEY": "sk_test_x",
        "VAMOS_AGENT": "remi",
    }
    with pytest.raises(ConfigError, match="VAMOS_VENDOR"):
        build_app_from_env(base)
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_app_from_env({**base, "VAMOS_VENDOR": "openai_realtime"})
