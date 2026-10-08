"""Smoke: the real server running the test-only inbound agent over /voice, with a fake provider.

The fake provider plays a model for an agent that speaks first: no screen pop,
the greeting goes out before the caller says anything, the account tool is
refused until the caller is verified, then the call ends. No database, no
vendor, no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Self

from fastapi.testclient import TestClient

from vamos_voice.context import AgentContext
from vamos_voice.providers.base import CallRuntime, VoiceProvider
from vamos_voice.server import create_app

from .fake_inbound import FakeInboundAgent
from .fakes import HELLO

GREETING = b"\x09\x09" * 480


class InboundFakeProvider(VoiceProvider):
    name = "fake"
    trace_adapter = None
    required_env = ()

    def __init__(self) -> None:
        self.results: list[dict] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls()

    async def run_call(self, call: CallRuntime) -> None:
        heard = asyncio.Event()

        async def to_vendor(pcm: bytes) -> None:
            heard.set()

        call.inbound.bind(to_vendor)
        pump = asyncio.create_task(call.inbound.run())
        assert call.start.audio_open, "an inbound agent's line is never held"
        assert await call.outbound.send(GREETING, "greeting")
        await asyncio.wait_for(heard.wait(), 5)
        dispatch = call.session.dispatch
        self.results.append(await dispatch("get_account", {}))
        self.results.append(
            await dispatch("verify_caller", {"account_number": "ACME-1", "postal_code": "10001"})
        )
        self.results.append(await dispatch("get_account", {}))
        self.results.append(await dispatch("end_call", {}))
        assert call.session.ended
        await call.hang_up()
        pump.cancel()


def test_inbound_agent_greets_first_and_gates_its_tools() -> None:
    agent = FakeInboundAgent.from_env({}, AgentContext.from_json(None))
    provider = InboundFakeProvider()
    with TestClient(create_app(agent, provider)) as client:
        assert client.get("/health").json() == {"status": "ok", "agent": "fake_inbound", "vendor": "fake"}
        with client.websocket_connect("/voice") as ws:
            assert ws.receive_bytes() == GREETING, "the agent speaks before the caller"
            ws.send_bytes(HELLO)
            assert ws.receive()["type"] == "websocket.close"
    refused, verified, account, ended = provider.results
    assert refused == {"error": "Verify the caller first with verify_caller."}
    assert verified == {"verified": True}
    assert account["account"] == "ACME-1" and account["as_of"].startswith("2027-03-01T10:00")
    assert ended == {"ended": True}
