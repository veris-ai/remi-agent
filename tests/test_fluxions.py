"""Fluxions realtime: the session, the soul cap and the playback buffer, pinned offline.

The parity matrix covers the shared contract; these cover what is Fluxions'
own: the host's soul cap (refused before connecting, and a truncated soul in
``session.created`` fails the call), the full voice id, 16 kHz caller audio,
the playback buffer the vendor expects (paced release, ``audio.flush``,
``playback.pos``) and a hang-up at the end of the agent's turn.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from vamos_voice.providers import fluxions as fx
from vamos_voice.providers.fluxions import FluxionsProvider

from .fakes import HELLO, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime

SECOND = b"\x00\x01" * 24_000  # one second of agent audio at 24 kHz


def provider(sock: FakeVendorSocket | None = None) -> FluxionsProvider:
    p = FluxionsProvider("fx-key", "https://fluxions.test/vui", "maeve")
    if sock is not None:
        p.connect = sock.connector()
    return p


def created(update: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "type": "session.created",
        "session_id": "s1",
        "model": "vllm:test",
        "mic_sample_rate": 16_000,
        "audio_sample_rate": 24_000,
        "soul": update["soul"],
        "client_tools": [t["name"] for t in update["tools"]],
        "client_tools_rejected": [],
        "denied": [],
        **changes,
    }


async def start(sock: FakeVendorSocket, line: FakeLine | None = None, **changes: Any):
    line, session = line or FakeLine(), ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider(sock).run_call(call))
    update = await sock.wait_for(lambda f: f.get("type") == "session.update")
    sock.push(created(update, **changes))
    return call, line, session, task


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_from_env_and_the_socket_url() -> None:
    p = FluxionsProvider.from_env({"FLUXIONS_API_KEY": "k"})
    assert p.url == "wss://api.fluxions.ai/vui/v1/realtime" and p.voice == "maeve"
    with pytest.raises(ValueError, match="https"):
        FluxionsProvider.from_env({"FLUXIONS_API_KEY": "k", "VAMOS_FLUXIONS_BASE_URL": "http://x"})


async def test_warm_up_reads_the_soul_cap_and_resolves_the_full_voice_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"max_soul_chars": 4000, "model": "m", "auth_required": False})
        return httpx.Response(200, json={"voices": [{"id": "maeve", "voice_id": "maeve.h1"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        fx.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    p = provider()
    await p.warm_up()
    assert (p.max_soul_chars, p.voice_id) == (4000, "maeve.h1")
    assert seen == [("/vui/config", "fx-key"), ("/vui/voices", "fx-key")], "bare key, no Bearer"
    assert p.session_update(REMI.setup())["voice"] == "maeve.h1"


async def test_instructions_over_the_soul_cap_are_refused_before_connecting() -> None:
    sock = FakeVendorSocket()
    p = provider(sock)
    p.max_soul_chars = 4000
    call = runtime(REMI)
    assert len(call.setup.instructions) > 4000
    with pytest.raises(RuntimeError, match="truncated prompt"):
        await p.run_call(call)
    assert not hasattr(sock, "url"), "never connected"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"soul": "short"}, "soul truncated"),
        ({"client_tools_rejected": ["end_call"]}, "rejected tools"),
        ({"denied": ["soul", "tools"]}, "denied"),
        ({"mic_sample_rate": 24_000}, "rates"),
    ],
)
async def test_a_session_that_differs_from_the_one_sent_fails_the_call(changes: dict, message: str) -> None:
    sock = FakeVendorSocket()
    _, _, _, task = await start(sock, **changes)
    with pytest.raises(RuntimeError, match=message):
        await asyncio.wait_for(task, 3)


async def test_session_update_knobs_and_header() -> None:
    sock = FakeVendorSocket()
    call, _, _, task = await start(sock)
    update = sock.sent[0]
    assert update["soul"] == call.setup.instructions
    assert (update["greet"], update["allow_hangup"], update["echo_guard"]) == (False, False, False)
    assert update["idle_check_s"] == update["idle_hangup_s"] == 3600
    assert sock.headers == {"Authorization": "fx-key"} and "token" not in update
    await stop(task)


async def test_caller_audio_goes_up_at_16_khz() -> None:
    sock = FakeVendorSocket()
    line = FakeLine()
    await line.incoming.put(HELLO)  # 200 ms at 24 kHz
    _, _, _, task = await start(sock, line)
    frame = await sock.wait_for(lambda f: "binary" in f)
    assert abs(len(frame["binary"]) - len(HELLO) * 2 // 3) <= 4
    await stop(task)


async def test_agent_audio_is_paced_and_audio_flush_drops_what_was_not_released() -> None:
    sock = FakeVendorSocket()
    _, line, _, task = await start(sock)
    sock.push({"type": "user.transcript", "text": "Hello?"})
    for _ in range(5):
        sock.push_binary(SECOND)  # five seconds, delivered at once
    await asyncio.sleep(0.3)
    released = sum(map(len, line.sent)) / len(SECOND)
    assert 1 <= released <= 2.1, f"{released:.1f} s released at once; the lead is {fx.PLAYBACK_LEAD_S} s"
    sock.push({"type": "audio.flush"})
    await asyncio.sleep(1.5)
    assert sum(map(len, line.sent)) / len(SECOND) == released, "flushed audio still reached the caller"
    position = await sock.wait_for(lambda f: f.get("type") == "playback.pos")
    assert 0 <= position["played_s"] <= released
    await stop(task)


async def test_tool_arguments_as_a_string_or_an_object_and_hang_up_at_the_end_of_the_turn() -> None:
    sock = FakeVendorSocket()
    _, line, session, task = await start(sock)
    sock.push({"type": "user.transcript", "text": "Bye."})
    sock.push({"type": "agent.sentence", "text": "Goodbye.", "idx": 0})
    sock.push({"type": "tool.call", "call_id": "c1", "name": "get_account", "arguments": '{"a": 1}'})
    sock.push({"type": "tool.call", "call_id": "c2", "name": "end_call", "arguments": {}})
    results = [await sock.wait_for(lambda f, i=i: f.get("call_id") == i) for i in ("c1", "c2")]
    assert [json.loads(r["output"]) for r in results] == [{"ok": "get_account"}, {"ok": "end_call"}]
    assert session.calls == [("get_account", {"a": 1}), ("end_call", {})]
    await asyncio.sleep(0.05)
    assert not line.closed, "hung up in the middle of the agent's turn"
    sock.push({"type": "agent.done", "cancelled": False})
    await asyncio.wait_for(task, 3)
    assert line.closed
