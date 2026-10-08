"""Deepgram Voice Agent: the frames this provider sends and how it reacts, pinned offline.

The parity matrix covers the shared contract (instructions, tools, call start,
end_call); these cover what is Deepgram's own: the one ``Settings`` message,
audio held until ``SettingsApplied``, binary audio both ways, barge-in, and a
hang-up that waits for the agent to finish speaking.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from .fakes import HELLO, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime

pytest.importorskip("vamos_voice.providers.deepgram")

from vamos_voice.providers.deepgram import DeepgramProvider

AUDIO = b"\x00\x01" * 240


def provider(sock: FakeVendorSocket) -> DeepgramProvider:
    p = DeepgramProvider("dg-key", "nova-3", "gpt-4.1-mini", "aura-2-thalia-en")
    p.connect = sock.connector()
    return p


async def start(sock: FakeVendorSocket, line: FakeLine | None = None, session: ScriptedSession | None = None):
    line, session = line or FakeLine(), session or ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider(sock).run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "Settings")
    return call, line, session, task


def test_from_env_defaults_match_the_energy_candidate() -> None:
    p = DeepgramProvider.from_env({"DEEPGRAM_API_KEY": "k"})
    assert (p.listen_model, p.llm_model, p.voice) == ("nova-3", "gpt-4.1-mini", "aura-2-thalia-en")
    p = DeepgramProvider.from_env({"DEEPGRAM_API_KEY": "k", "VAMOS_DEEPGRAM_LLM_MODEL": "gpt-4.1"})
    assert p.llm_model == "gpt-4.1"


def test_settings_is_the_whole_session_with_client_side_functions() -> None:
    setup = REMI.setup()
    settings = DeepgramProvider("k", "nova-3", "gpt-4.1-mini", "aura-2-thalia-en").settings(setup)
    pcm = {"encoding": "linear16", "sample_rate": 24_000}
    assert settings["audio"] == {"input": pcm, "output": {**pcm, "container": "none"}}
    agent = settings["agent"]
    assert "greeting" not in agent, "a callee-first agent must not greet"
    assert agent["think"]["provider"] == {"type": "open_ai", "model": "gpt-4.1-mini"}
    assert agent["think"]["prompt"] == setup.instructions
    assert all("endpoint" not in f for f in agent["think"]["functions"]), "no endpoint = client-side"
    assert [f["name"] for f in agent["think"]["functions"]] == [t.name for t in setup.tools]
    assert "dg-key" not in json.dumps(settings)


async def test_caller_audio_waits_for_settings_applied_and_passes_through_unchanged() -> None:
    sock = FakeVendorSocket()
    line = FakeLine()
    await line.incoming.put(HELLO)
    _, _, _, task = await start(sock, line)
    assert sock.headers == {"Authorization": "Token dg-key"}
    await asyncio.sleep(0.05)
    assert not any("binary" in f for f in sock.sent), "audio before SettingsApplied is discarded by Deepgram"
    sock.push({"type": "Welcome", "request_id": "r"})
    sock.push({"type": "SettingsApplied"})
    frame = await sock.wait_for(lambda f: "binary" in f)
    assert frame["binary"] == HELLO, "24 kHz linear16 both ways: no resampling"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_barge_in_marks_the_playout_interrupted() -> None:
    sock = FakeVendorSocket()
    call, _, _, task = await start(sock)
    interrupted = []
    call.outbound.interrupted = lambda: interrupted.append(True)
    sock.push({"type": "SettingsApplied"})
    sock.push({"type": "UserStartedSpeaking"})
    await asyncio.sleep(0.05)
    assert interrupted == [True]
    assert not any(f.get("type") not in ("Settings", None) for f in sock.sent if "binary" not in f)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_end_call_hangs_up_once_the_agent_has_finished_speaking() -> None:
    sock = FakeVendorSocket()
    _, line, _, task = await start(sock)
    sock.push({"type": "SettingsApplied"})
    sock.push({"type": "ConversationText", "role": "user", "content": "Hello?"})
    sock.push_binary(AUDIO)  # "Goodbye." still streaming
    fn = {"id": "f1", "name": "end_call", "arguments": "{}", "client_side": True}
    sock.push({"type": "FunctionCallRequest", "functions": [fn]})
    response = await sock.wait_for(lambda f: f.get("type") == "FunctionCallResponse")
    assert response == {
        "type": "FunctionCallResponse",
        "id": "f1",
        "name": "end_call",
        "content": '{"ok": "end_call"}',
    }
    await asyncio.sleep(0.05)
    assert not line.closed, "hung up over the agent's last words"
    sock.push({"type": "AgentAudioDone"})
    await asyncio.wait_for(task, 3)
    assert line.closed and line.sent


async def test_server_side_functions_are_skipped_and_cut_arguments_are_refused() -> None:
    sock = FakeVendorSocket()
    _, _, session, task = await start(sock)
    sock.push({"type": "SettingsApplied"})
    functions = [
        {"id": "s", "name": "web_search", "arguments": "{}", "client_side": False},
        {"id": "c", "name": "get_account", "arguments": '{"acc', "client_side": True},
    ]
    sock.push({"type": "FunctionCallRequest", "functions": functions})
    response = await sock.wait_for(lambda f: f.get("type") == "FunctionCallResponse")
    assert response["id"] == "c" and "error" in json.loads(response["content"])
    assert session.calls == []
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_vendor_error_fails_the_call() -> None:
    sock = FakeVendorSocket()
    _, _, _, task = await start(sock)
    sock.push({"type": "SettingsApplied"})
    sock.push({"type": "Error", "code": "UNPARSABLE_CLIENT_MESSAGE", "description": "bad"})
    with pytest.raises(RuntimeError, match="UNPARSABLE_CLIENT_MESSAGE"):
        await asyncio.wait_for(task, 3)
