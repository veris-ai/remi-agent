"""Phonic speech-to-speech: the frames this provider sends and how it reacts, pinned offline.

The parity matrix covers the shared contract (instructions, tools, call start,
end_call); these cover what is Phonic's own: the one ``config`` (the field the
trace adapter reads, uninterruptible sync tools, the built-in
``choose_not_to_respond``, no welcome message, no VAD setting), caller audio in
20 ms base64 chunks after ``ready_to_start_conversation``, tool results on their
``tool_call_id``, barge-in, the hang-up after the last words, and failures
(an ``error`` event, an abnormal close).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets

from vamos_voice.providers.phonic import CHUNK_BYTES, PhonicProvider

from .fakes import AGENT_CASES, REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime

AUDIO = b"\x00\x01" * 240


def provider(sock: FakeVendorSocket) -> PhonicProvider:
    p = PhonicProvider("ph-key", "phonic-1.1", "sabrina")
    p.connect = sock.connector()
    return p


async def start(sock: FakeVendorSocket, line: FakeLine | None = None, session: ScriptedSession | None = None):
    line, session = line or FakeLine(), session or ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider(sock).run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "config")
    return call, line, session, task


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_from_env_defaults() -> None:
    p = PhonicProvider.from_env({"PHONIC_API_KEY": "k"})
    assert (p.model, p.voice) == ("phonic-1.1", "sabrina")
    p = PhonicProvider.from_env(
        {"PHONIC_API_KEY": "k", "VAMOS_PHONIC_MODEL": "phonic-1", "VAMOS_PHONIC_VOICE": "v"}
    )
    assert (p.model, p.voice) == ("phonic-1", "v")


def test_config_matches_the_energy_candidate_where_remi_allows() -> None:
    setup = REMI.setup()
    config = PhonicProvider("ph-key", "phonic-1.1", "sabrina").config(setup)
    assert config["type"] == "config" and config["system_prompt"] == setup.instructions
    # The bench's ``phonic`` trace adapter reads ``phonic_model`` (``model`` is the newer name).
    assert config["phonic_model"] == "phonic-1.1" and "model" not in config
    assert config["intelligence_level"] == "high" and config["voice_id"] == "sabrina"
    assert config["input_format"] == config["output_format"] == "pcm_24000"
    assert "welcome_message" not in config, "a callee-first agent must not greet"
    assert not [k for k in config if k.startswith("vad_") or k == "turn_taking"], "Phonic's own turn taking"
    *inline, built_in = config["tools"]
    assert built_in == {
        "type": "built_in",
        "name": "choose_not_to_respond",
        "tool_config": {"respond_after_sec": 3},
    }
    assert [t["tool_schema"]["function"]["name"] for t in inline] == [t.name for t in setup.tools]
    for tool in inline:
        assert tool["type"] == "custom_websocket"
        assert tool["execution_mode"] == "sync" and tool["uninterruptible"] is True
        assert tool["tool_call_output_timeout_ms"] == 30_000
        # Phonic's InlineWebSocketTool schema: OpenAIFunction.strict is required and must be true.
        assert tool["tool_schema"]["function"]["strict"] is True
    assert "ph-key" not in json.dumps(config)


async def test_caller_audio_waits_for_ready_and_goes_up_in_20_ms_chunks() -> None:
    sock = FakeVendorSocket()
    line = FakeLine()
    frame = bytes(range(256)) * 10  # 2,560 bytes: 53 ms
    await line.incoming.put(frame)
    _, _, _, task = await start(sock, line)
    assert sock.headers == {"Authorization": "Bearer ph-key"} and sock.url == "wss://api.phonic.ai/v1/sts/ws"
    await asyncio.sleep(0.05)
    assert not any(f.get("type") == "audio_chunk" for f in sock.sent), (
        "audio before the conversation is ready"
    )
    sock.push({"type": "ready_to_start_conversation"})
    await sock.wait_for(lambda f: f.get("type") == "audio_chunk")
    await asyncio.sleep(0.05)
    chunks = [base64.b64decode(f["audio"]) for f in sock.sent if f.get("type") == "audio_chunk"]
    assert b"".join(chunks) == frame, "24 kHz PCM16 both ways: no resampling"
    assert max(map(len, chunks)) == CHUNK_BYTES, "Phonic rejects chunks over 40 ms"
    await stop(task)


async def test_tool_calls_are_answered_on_their_id_in_order() -> None:
    sock = FakeVendorSocket()
    _, _, session, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "tool_call", "tool_call_id": "a", "tool_name": "get_account", "parameters": {}})
    sock.push(
        {"type": "tool_call", "tool_call_id": "b", "tool_name": "get_debt_details", "parameters": {"x": 1}}
    )
    await sock.wait_for(lambda f: f.get("tool_call_id") == "b")
    outputs = [f for f in sock.sent if f.get("type") == "tool_call_output"]
    assert [o["tool_call_id"] for o in outputs] == ["a", "b"]
    assert outputs[0]["output"] == {"ok": "get_account"}, "the tool's dict, as an object"
    assert session.calls == [("get_account", {}), ("get_debt_details", {"x": 1})]
    await stop(task)


async def test_agent_audio_reaches_the_caller_once_the_callee_spoke_and_barge_in_drops_nothing() -> None:
    sock = FakeVendorSocket()
    call, line, _, task = await start(sock)
    interrupted = []
    call.outbound.interrupted = lambda **kw: interrupted.append(kw) or {}
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "input_text", "language": "en", "text": "Hello?"})
    sock.push({"type": "audio_chunk", "audio": base64.b64encode(AUDIO).decode(), "text": "Hi, "})
    await asyncio.sleep(0.05)
    assert line.sent == [AUDIO]
    sock.push({"type": "interrupted_response", "text": "Hi, "})
    await asyncio.sleep(0.05)
    assert interrupted == [{}], "Phonic keeps what it said in context: nothing is dropped"
    await stop(task)


async def test_end_call_hangs_up_after_the_last_words() -> None:
    sock = FakeVendorSocket()
    _, line, _, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "input_text", "language": "en", "text": "Bye."})
    sock.push({"type": "tool_call", "tool_call_id": "e", "tool_name": "end_call", "parameters": {}})
    output = await sock.wait_for(lambda f: f.get("type") == "tool_call_output")
    assert output == {"type": "tool_call_output", "tool_call_id": "e", "output": {"ok": "end_call"}}
    sock.push({"type": "audio_chunk", "audio": base64.b64encode(AUDIO).decode(), "text": "Goodbye."})
    await asyncio.sleep(0.3)
    assert not line.closed, "hung up over the agent's last words"
    await asyncio.wait_for(task, 5)
    assert line.closed and line.sent


async def test_phonic_ending_the_conversation_hangs_up() -> None:
    sock = FakeVendorSocket()
    _, line, _, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "assistant_ended_conversation"})
    await asyncio.wait_for(task, 5)
    assert line.closed


async def test_an_agent_that_opens_asks_for_a_reply_with_its_opening() -> None:
    case = next(c for c in AGENT_CASES if c.policy.agent_opens)
    sock = FakeVendorSocket()
    call = runtime(case, FakeLine(), ScriptedSession())
    task = asyncio.create_task(provider(sock).run_call(call))
    config = await sock.wait_for(lambda f: f.get("type") == "config")
    assert "welcome_message" not in config
    sock.push({"type": "ready_to_start_conversation"})
    request = await sock.wait_for(lambda f: f.get("type") == "generate_reply")
    assert request == {"type": "generate_reply", "system_message": case.policy.opening_instruction}
    await stop(task)


async def test_an_error_before_ready_fails_the_call() -> None:
    sock = FakeVendorSocket()
    _, _, _, task = await start(sock)
    sock.push(
        {"type": "error", "error": {"message": "invalid voice"}, "param_errors": {"voice_id": "unknown"}}
    )
    with pytest.raises(RuntimeError, match="invalid voice"):
        await asyncio.wait_for(task, 3)


async def test_an_error_during_the_call_fails_it() -> None:
    sock = FakeVendorSocket()
    _, _, _, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "error", "error": {"message": "boom"}})
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(task, 3)


async def test_an_abnormal_close_fails_the_call() -> None:
    class ClosingSocket(FakeVendorSocket):
        async def recv(self):
            message = await self.inbox.get()
            if message == "close-4505":
                from websockets.frames import Close

                raise websockets.ConnectionClosedError(Close(4505, "max duration"), None)
            return message

    sock = ClosingSocket()
    _, _, _, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.inbox.put_nowait("close-4505")
    with pytest.raises(RuntimeError, match="abnormally"):
        await asyncio.wait_for(task, 3)


async def test_warnings_are_logged_and_the_call_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    sock = FakeVendorSocket()
    _, _, _, task = await start(sock)
    sock.push({"type": "ready_to_start_conversation"})
    sock.push({"type": "warning", "warning": {"message": "slow tool"}})
    await asyncio.sleep(0.05)
    assert not task.done() and "slow tool" in caplog.text
    await stop(task)
