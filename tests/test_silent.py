"""The silent baseline: answers like any vendor, then never speaks, calls no tool and never hangs up."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from vamos_voice.context import AgentContext
from vamos_voice.framing import Framing
from vamos_voice.providers import load_provider
from vamos_voice.providers.silent import SilentProvider
from vamos_voice.server import create_app

from .fake_inbound import FakeInboundAgent
from .fakes import AGENT_CASES, HELLO, AgentCase, FakeLine, ScriptedSession, runtime


def test_it_is_a_vendor_the_image_can_select() -> None:
    assert load_provider("silent") is SilentProvider
    assert SilentProvider.required_env == ()
    assert SilentProvider.trace_adapter is None


@pytest.mark.parametrize("case", AGENT_CASES, ids=lambda c: c.name)
async def test_it_listens_in_silence_until_the_caller_hangs_up(case: AgentCase) -> None:
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(SilentProvider.from_env({}).run_call(call))
    for _ in range(5):
        await line.incoming.put(HELLO)
    await asyncio.sleep(0.2)
    assert line.incoming.empty(), "caller audio is consumed"
    assert not task.done() and not line.closed, "it never hangs up on its own"
    await line.incoming.put(None)  # the caller hangs up
    await asyncio.wait_for(task, 1)
    assert line.sent == [] and call.outbound.sent_bytes == 0, "no agent audio"
    assert session.calls == [], "no tool calls"
    assert not call.hung_up


def test_over_the_real_voice_endpoint_nothing_comes_back_until_the_caller_ends() -> None:
    agent = FakeInboundAgent.from_env({}, AgentContext.from_json(None))
    with TestClient(create_app(agent, SilentProvider(), framing=Framing.JSON)) as client:
        assert client.get("/health").json() == {"status": "ok", "agent": "fake_inbound", "vendor": "silent"}
        with client.websocket_connect("/voice") as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"type": "audio", "audio": base64.b64encode(HELLO).decode()}))
            ws.send_text('{"type": "end"}')
            message = ws.receive()
            assert message["type"] == "websocket.close", f"the silent agent sent {message}"
