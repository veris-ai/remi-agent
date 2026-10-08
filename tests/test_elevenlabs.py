"""ElevenLabs Conversational AI: the per-call agent and the socket protocol, pinned offline.

The parity matrix covers the shared contract; these cover what is ElevenLabs'
own: the stored agent created per call (and always deleted), parameter
descriptions it requires, the prompt override the trace reads, ping/pong, the
audio-format check and a hang-up that lets the last audio arrive.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from vamos_voice.tools import ToolSpec

from .fakes import REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime

pytest.importorskip("httpx")

from vamos_voice.providers import elevenlabs as el
from vamos_voice.providers.elevenlabs import ElevenLabsProvider, ElevenLabsToolFormat


class Store:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    async def create(self, body: dict[str, Any]) -> str:
        self.created.append(body)
        return "agent_1"

    async def delete(self, agent_id: str) -> None:
        self.deleted.append(agent_id)


def metadata(fmt: str = "pcm_24000") -> dict[str, Any]:
    return {
        "type": "conversation_initiation_metadata",
        "conversation_initiation_metadata_event": {
            "conversation_id": "c1",
            "user_input_audio_format": fmt,
            "agent_output_audio_format": fmt,
        },
    }


async def start(sock: FakeVendorSocket, line: FakeLine | None = None):
    p = ElevenLabsProvider("el-key", "gpt-4.1-mini", "eleven_flash_v2", "voice")
    p.connect = sock.connector()
    p.agents = store = Store()
    line, session = line or FakeLine(), ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(p.run_call(call))
    await sock.wait_for(lambda f: f.get("type") == "conversation_initiation_client_data")
    return call, line, session, store, task


def test_from_env_defaults_match_the_energy_candidate() -> None:
    p = ElevenLabsProvider.from_env({"ELEVENLABS_API_KEY": "k"})
    assert (p.llm, p.tts_model, p.voice_id) == ("gpt-4.1-mini", "eleven_flash_v2", "EXAVITQu4vr4xnSDxMaL")


def test_every_parameter_gets_a_description_and_existing_ones_are_kept() -> None:
    spec = ToolSpec(
        "take_payment",
        "Charge it.",
        {
            "type": "object",
            "properties": {
                "amount_cents": {"type": "integer", "exclusiveMinimum": 0},
                "method_id": {"type": "string", "description": "From add_payment_method."},
                "card": {"type": "object", "properties": {"last4": {"type": "string"}}},
            },
            "required": ["amount_cents"],
        },
    )
    (tool,) = ElevenLabsToolFormat().convert([spec])
    props = tool["parameters"]["properties"]
    assert tool["type"] == "client" and tool["expects_response"] is True
    assert props["amount_cents"]["description"] == "amount_cents"
    assert props["method_id"]["description"] == "From add_payment_method."
    assert props["card"]["properties"]["last4"]["description"] == "last4"
    assert "description" not in tool["parameters"], "only parameters, not the schema root"


def test_the_per_call_agent_carries_the_call_and_allows_the_overrides() -> None:
    setup = REMI.setup()
    body = ElevenLabsProvider("k", "gpt-4.1-mini", "eleven_flash_v2", "v").agent_body(setup)
    config = body["conversation_config"]
    assert config["agent"]["first_message"] == ""
    assert config["agent"]["prompt"]["prompt"] == setup.instructions
    assert config["agent"]["prompt"]["llm"] == "gpt-4.1-mini"
    assert (
        config["tts"]["agent_output_audio_format"] == config["asr"]["user_input_audio_format"] == "pcm_24000"
    )
    overrides = body["platform_settings"]["overrides"]["conversation_config_override"]["agent"]
    assert overrides == {"prompt": {"prompt": True}, "first_message": True}


async def test_the_socket_opens_with_the_prompt_override_and_the_key_in_a_header() -> None:
    sock = FakeVendorSocket()
    call, _, _, store, task = await start(sock)
    assert sock.url.endswith("?agent_id=agent_1") and "el-key" not in sock.url
    assert sock.headers == {"xi-api-key": "el-key"}
    first = sock.sent[0]
    assert first["conversation_config_override"]["agent"] == {
        "prompt": {"prompt": call.setup.instructions},
        "first_message": "",
    }
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert store.deleted == ["agent_1"], "the per-call agent is deleted when the call ends"


async def test_ping_is_answered_and_a_tool_result_is_a_string() -> None:
    sock = FakeVendorSocket()
    _, _, session, _, task = await start(sock)
    sock.push(metadata())
    sock.push({"type": "ping", "ping_event": {"event_id": 7, "ping_ms": 40}})
    assert await sock.wait_for(lambda f: f.get("type") == "pong") == {"type": "pong", "event_id": 7}
    call = {"tool_name": "get_account", "tool_call_id": "t1", "parameters": {"account_id": "TAS-1"}}
    sock.push({"type": "client_tool_call", "client_tool_call": call})
    result = await sock.wait_for(lambda f: f.get("type") == "client_tool_result")
    assert result == {
        "type": "client_tool_result",
        "tool_call_id": "t1",
        "result": json.dumps({"ok": "get_account"}),
        "is_error": False,
    }
    assert session.calls == [("get_account", {"account_id": "TAS-1"})]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_wrong_audio_format_fails_the_call_and_the_agent_is_still_deleted() -> None:
    sock = FakeVendorSocket()
    _, _, _, store, task = await start(sock)
    sock.push(metadata("pcm_16000"))
    with pytest.raises(RuntimeError, match="pcm_16000"):
        await asyncio.wait_for(task, 3)
    assert store.deleted == ["agent_1"]


async def test_end_call_lets_the_last_audio_arrive_before_hanging_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(el, "HANGUP_QUIET_S", 0.2)
    sock = FakeVendorSocket()
    _, line, _, _, task = await start(sock)
    sock.push(metadata())
    sock.push({"type": "user_transcript", "user_transcription_event": {"user_transcript": "Bye."}})
    sock.push(
        {"type": "client_tool_call", "client_tool_call": {"tool_name": "end_call", "tool_call_id": "e"}}
    )
    await sock.wait_for(lambda f: f.get("type") == "client_tool_result")
    goodbye = base64.b64encode(b"\x00\x01" * 2400).decode()
    sock.push({"type": "audio", "audio_event": {"audio_base_64": goodbye, "event_id": 2}})
    await asyncio.wait_for(task, 3)
    assert line.closed and b"".join(line.sent) == b"\x00\x01" * 2400
