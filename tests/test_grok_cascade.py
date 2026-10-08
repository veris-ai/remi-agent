"""Grok STT + TTS cascade: the LLM routes, xAI's STT and TTS frames, and the streamed chat round, offline.

The parity matrix (``test_parity.py``) covers the call through the shared
cascade; these pin xAI's legs and the ``openai`` SDK chat leg the Hugging
Face cascade shares.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("openai")

import httpx2
from openai import AsyncOpenAI

from vamos_voice.providers.grok_cascade import (
    GrokCascadeConfig,
    GrokCascadeProvider,
    GrokSTT,
    GrokTTS,
)
from vamos_voice.providers.openai_chat import OpenAIChat

from .fakes import REMI, FakeLine, FakeVendorSocket, ScriptedSession, runtime

XAI = {"XAI_API_KEY": "xai-k"}


def test_the_default_llm_is_gpt_4_1_mini_on_openai_and_needs_its_key() -> None:
    assert GrokCascadeProvider.required_env == ("XAI_API_KEY",)
    with pytest.raises(ValueError, match="needs OPENAI_API_KEY"):
        GrokCascadeConfig.from_env(XAI)
    provider = GrokCascadeProvider.from_env({**XAI, "OPENAI_API_KEY": "sk-k"})
    assert provider.llm.model == "gpt-4.1-mini" and provider.llm.extra == {}
    assert str(provider.llm.client.base_url) == "https://api.openai.com/v1/"
    assert provider.llm.client.api_key == "sk-k"


def test_grok_4_3_runs_on_xai_with_reasoning_off() -> None:
    provider = GrokCascadeProvider.from_env({**XAI, "VAMOS_GROK_CASCADE_LLM": "grok-4.3"})
    assert provider.llm.model == "grok-4.3" and provider.llm.extra == {"reasoning_effort": "none"}
    assert str(provider.llm.client.base_url) == "https://api.x.ai/v1/"
    assert provider.llm.client.api_key == "xai-k"


def test_an_unknown_llm_stops_startup() -> None:
    with pytest.raises(ValueError, match="VAMOS_GROK_CASCADE_LLM"):
        GrokCascadeConfig.from_env({**XAI, "VAMOS_GROK_CASCADE_LLM": "grok-9"})


def test_stt_uses_smart_turn_and_tts_streams_24_khz_pcm() -> None:
    config = GrokCascadeConfig.from_env({**XAI, "OPENAI_API_KEY": "sk"})
    stt = urlparse(config.stt_url())
    assert (stt.scheme, stt.netloc, stt.path) == ("wss", "api.x.ai", "/v1/stt")
    assert {k: v[0] for k, v in parse_qs(stt.query).items()} == {
        "model": "grok-voice-transcribe-2.0",
        "encoding": "pcm",
        "sample_rate": "24000",
        "language": "en",
        "format": "true",
        "smart_turn": "0.7",
        "smart_turn_timeout": "3000",
    }
    tts = urlparse(config.tts_url())
    assert tts.path == "/v1/tts"
    assert {k: v[0] for k, v in parse_qs(tts.query).items()} == {
        "voice": "carina",
        "language": "en",
        "codec": "pcm",
        "sample_rate": "24000",
    }
    assert "xai-k" not in config.stt_url() + config.tts_url() + repr(config)


async def test_stt_sends_the_callers_audio_unchanged_and_only_utterance_finals_are_turns() -> None:
    sock = FakeVendorSocket()
    stt = GrokSTT(sock)
    sock.push({"type": "transcript.created", "id": "t1"})
    await stt.start()
    await stt.feed(b"\x01\x02" * 480, speaking=True)
    assert sock.sent == [{"binary": b"\x01\x02" * 480}]

    turns: list[str] = []

    async def on_turn(text: str) -> None:
        turns.append(text)

    sock.push({"type": "transcript.partial", "text": "four four", "is_final": False, "speech_final": False})
    sock.push(
        {"type": "transcript.partial", "text": "four four seven", "is_final": True, "speech_final": False}
    )
    sock.push(
        {
            "type": "transcript.partial",
            "text": "447 129",
            "is_final": True,
            "speech_final": True,
            "end_of_turn_confidence": 0.9,
        }
    )
    sock.inbox.put_nowait(None)  # xAI closes
    await stt.run(on_turn)
    assert turns == ["447 129"]


async def test_stt_must_open_with_transcript_created() -> None:
    sock = FakeVendorSocket()
    sock.push({"type": "error", "message": "unauthorized"})
    with pytest.raises(RuntimeError, match="unauthorized"):
        await GrokSTT(sock).start()


async def test_tts_streams_text_deltas_and_carries_a_stray_byte_to_the_next_chunk() -> None:
    sock = FakeVendorSocket()
    tts = GrokTTS(sock)

    async def text():
        for delta in ("Hi ", "Marcus."):
            yield delta

    sock.push({"type": "audio.delta", "delta": base64.b64encode(b"\x01\x02\x03").decode()})
    sock.push({"type": "audio.delta", "delta": base64.b64encode(b"\x04\x05\x06\x07\x08").decode()})
    sock.push({"type": "audio.done"})
    audio = [pcm async for pcm in tts.speak(text())]
    assert audio == [b"\x01\x02", b"\x03\x04\x05\x06\x07\x08"]
    assert sock.sent == [
        {"type": "text.delta", "delta": "Hi "},
        {"type": "text.delta", "delta": "Marcus."},
        {"type": "text.done"},
    ]


async def test_tts_clear_drops_audio_up_to_the_acknowledgement() -> None:
    sock = FakeVendorSocket()
    tts = GrokTTS(sock)
    sock.push({"type": "audio.delta", "delta": base64.b64encode(b"\x00\x00").decode()})
    sock.push({"type": "audio.clear"})
    sock.push({"type": "audio.delta", "delta": "next utterance"})
    await tts.clear()
    assert sock.sent == [{"type": "text.clear"}]
    assert sock.inbox.qsize() == 1, "nothing after audio.clear is consumed"


async def test_tts_error_fails_the_utterance() -> None:
    sock = FakeVendorSocket()
    sock.push({"type": "error", "message": "voice not found"})

    async def text():
        yield "Hi."

    with pytest.raises(RuntimeError, match="voice not found"):
        [pcm async for pcm in GrokTTS(sock).speak(text())]


async def test_open_legs_puts_the_key_in_headers_on_both_sockets() -> None:
    sockets = [FakeVendorSocket(), FakeVendorSocket()]
    opened: list[tuple[str, dict[str, str]]] = []
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connect(url: str, headers: dict[str, str]):
        opened.append((url, headers))
        yield sockets[len(opened) - 1]

    provider = GrokCascadeProvider(GrokCascadeConfig(xai_api_key="xai-k", llm_api_key="sk"))
    provider.connect = connect  # type: ignore[method-assign,assignment]
    sockets[0].push({"type": "transcript.created", "id": "t"})
    async with provider.open_legs(runtime(REMI, FakeLine(), ScriptedSession())) as (stt, tts):
        assert isinstance(stt, GrokSTT) and isinstance(tts, GrokTTS)
    assert [urlparse(u).path for u, _ in opened] == ["/v1/stt", "/v1/tts"]
    assert all(h == {"Authorization": "Bearer xai-k"} for _, h in opened)


# --- the openai SDK chat leg (shared with the Hugging Face cascade) ------------------------


def sse(*chunks: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


async def test_a_streamed_round_yields_text_as_it_comes_and_assembles_tool_call_fragments() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        body = sse(
            chunk({"role": "assistant", "content": "Let me "}),
            chunk({"content": "check."}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_", "arguments": ""},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"name": "account", "arguments": '{"id": '}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"TAS-1"}'}}]}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "end_call", "arguments": "{}"},
                        }
                    ]
                }
            ),
        )
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = AsyncOpenAI(
        api_key="sk",
        base_url="https://api.x.ai/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    chat = OpenAIChat(client, "grok-4.3", {"reasoning_effort": "none"})
    tools = [{"type": "function", "function": {"name": "get_account", "description": "d", "parameters": {}}}]
    parts = [p async for p in chat.reply([{"role": "user", "content": "hi"}], tools)]
    assert parts[:2] == ["Let me ", "check."]
    assert [(c.id, c.name, c.arguments) for c in parts[2:]] == [
        ("call_1", "get_account", '{"id": "TAS-1"}'),
        ("call_2", "end_call", "{}"),
    ]
    (request,) = requests
    assert request["model"] == "grok-4.3" and request["stream"] is True
    assert request["tool_choice"] == "auto" and request["reasoning_effort"] == "none"
    assert request["tools"] == tools and request["messages"] == [{"role": "user", "content": "hi"}]
