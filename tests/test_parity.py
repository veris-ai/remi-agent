"""Parity: every installed vendor sends the same Remi.

Each case runs a provider through a whole scripted call offline and asserts on
what it actually SENT (the vendor's session/setup frame, its priming request,
its audio toward the caller), not on module attributes:

- byte-identical instructions, and exactly the registry's tools and schemas;
- the same turn-taking numbers mapped into each vendor's VAD;
- CALLEE_FIRST: the connect reads run first, the caller's "Hello?" is held
  until they finish, no greeting is triggered, and no agent audio reaches the
  caller before the callee has spoken;
- end_call hangs up the line.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from typing import Any

import httpx
import pytest

from remi_core.call_start import PRIME_INSTRUCTION, PRIME_NOTE
from remi_core.providers import load_provider, vendor_names
from remi_core.session import CONNECT_READS
from remi_core.tools import REGISTRY

from .fakes import HELLO, FakeLine, FakeVendorSocket, ScriptedSession, runtime, setup

EXPECTED = {
    s.name: (set(s.parameters["properties"]), set(s.parameters["required"])) for s in REGISTRY.specs()
}


def installed(name: str) -> bool:
    try:
        load_provider(name)
    except ImportError:
        return False
    return True


def test_every_vendor_folder_is_covered_here() -> None:
    assert set(vendor_names()) == {"openai_realtime", "gemini_live", "pipecat"}


@pytest.mark.parametrize("name", vendor_names())
def test_provider_metadata(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    provider = load_provider(name)
    assert provider.name == name
    assert provider.trace_adapter in {None, "openai-realtime", "gemini-live"}


# --- realtime-protocol vendors: a scripted vendor socket --------------------------------------


def openai_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vendors.openai_realtime import OpenAIRealtimeProvider

    provider = OpenAIRealtimeProvider("sk-test", "gpt-realtime", "marin", "gpt-4o-mini-transcribe")
    provider.connect = sock.connector()
    return {"provider": provider}


class OpenAIScript:
    """Server events in the OpenAI Realtime GA protocol."""

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def tool_calls(self, *names: str) -> None:
        self.n += 1
        for i, name in enumerate(names):
            self.sock.push(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": f"r{self.n}",
                    "item_id": f"fc{self.n}{i}",
                    "call_id": f"call_{self.n}_{i}",
                    "name": name,
                    "arguments": "{}"
                    if name != "log_contact_attempt"
                    else json.dumps({"answered_by": "consumer", "summary": "A short summary of the call."}),
                }
            )
        self.sock.push({"type": "response.done", "response": {"id": f"r{self.n}", "status": "completed"}})

    def audio(self) -> None:
        self.sock.push(
            {
                "type": "response.output_audio.delta",
                "response_id": "rA",
                "item_id": "itemA",
                "delta": base64.b64encode(b"\x00\x01" * 240).decode(),
            }
        )

    def callee_spoke(self) -> None:
        self.sock.push({"type": "input_audio_buffer.committed", "item_id": "u1"})

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        session = frame["session"]
        vad = session["audio"]["input"]["turn_detection"]
        return {
            "instructions": session["instructions"],
            "tools": {
                t["name"]: (set(t["parameters"]["properties"]), set(t["parameters"]["required"]))
                for t in session["tools"]
            },
            "silence_ms": vad["silence_duration_ms"],
            "prefix_ms": vad["prefix_padding_ms"],
            "transcribes_caller": bool(session["audio"]["input"]["transcription"]),
        }

    is_setup = staticmethod(lambda f: f.get("type") == "session.update")
    is_prime = staticmethod(lambda f: f.get("type") == "response.create")
    is_caller_audio = staticmethod(lambda f: f.get("type") == "input_audio_buffer.append")

    @staticmethod
    def check_prime(frame: dict[str, Any]) -> None:
        assert frame["response"]["tool_choice"] == "required"

    def ready(self) -> None:
        pass


class GeminiScript:
    """Server messages in the Gemini Live protocol."""

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        self.sock.push({"setupComplete": {}})

    def tool_calls(self, *names: str) -> None:
        calls = [
            {
                "id": f"fc_{self.n}_{i}",
                "name": n,
                "args": {}
                if n != "log_contact_attempt"
                else {"answered_by": "consumer", "summary": "A short summary of the call."},
            }
            for i, n in enumerate(names)
        ]
        self.n += 1
        self.sock.push({"toolCall": {"functionCalls": calls}})
        self.sock.push({"serverContent": {"turnComplete": True}})

    def audio(self) -> None:
        data = base64.b64encode(b"\x00\x01" * 240).decode()
        self.sock.push(
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": data}}]
                    }
                }
            }
        )

    def callee_spoke(self) -> None:
        self.sock.push({"serverContent": {"inputTranscription": {"text": "Hello?"}}})

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        s = frame["setup"]
        vad = s["realtimeInputConfig"]["automaticActivityDetection"]
        decls = s["tools"][0]["functionDeclarations"]
        return {
            "instructions": s["systemInstruction"]["parts"][0]["text"],
            "tools": {
                d["name"]: (set(d["parameters"]["properties"]), set(d["parameters"]["required"]))
                for d in decls
            },
            "silence_ms": vad["silenceDurationMs"],
            "prefix_ms": vad["prefixPaddingMs"],
            "transcribes_caller": "inputAudioTranscription" in s,
        }

    is_setup = staticmethod(lambda f: "setup" in f)
    is_prime = staticmethod(lambda f: "clientContent" in f)
    is_caller_audio = staticmethod(lambda f: "realtimeInput" in f)

    @staticmethod
    def check_prime(frame: dict[str, Any]) -> None:
        assert frame["clientContent"]["turns"][0]["parts"][0]["text"] == PRIME_NOTE


def gemini_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vendors.gemini_live import GeminiLiveProvider

    provider = GeminiLiveProvider("test-key", "gemini-test", "Puck")
    provider.connect = sock.connector()
    return {"provider": provider}


SOCKET_VENDORS = {"openai_realtime": (openai_call, OpenAIScript), "gemini_live": (gemini_call, GeminiScript)}


@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_sends_remi_and_waits_for_the_callee(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line, session = FakeLine(), ScriptedSession()
    call = runtime(line, session)
    task = asyncio.create_task(provider.run_call(call))

    sent_setup = script.setup(await sock.wait_for(script.is_setup))
    assert sent_setup["instructions"] == setup().instructions
    assert sent_setup["tools"] == EXPECTED
    assert (sent_setup["silence_ms"], sent_setup["prefix_ms"]) == (800, 300)
    assert sent_setup["transcribes_caller"]
    assert "x-goog-api-key" in sock.headers or "Authorization" in sock.headers
    assert "key=" not in sock.url

    script.ready()
    await line.incoming.put(HELLO)  # the callee answers while priming runs
    script.check_prime(await sock.wait_for(script.is_prime))
    await asyncio.sleep(0.05)
    assert not any(script.is_caller_audio(f) for f in sock.sent), (
        "caller audio reached the vendor before priming finished"
    )

    script.tool_calls(*CONNECT_READS)
    await sock.wait_for(script.is_caller_audio)  # the held "Hello?" is released after the reads
    assert [c for c, _ in session.calls] == list(CONNECT_READS)
    assert call.start.primed

    script.audio()  # the model talks before the callee has been heard
    await asyncio.sleep(0.05)
    assert line.sent == [], "agent audio reached the caller before the callee spoke"
    script.callee_spoke()
    script.audio()
    await asyncio.sleep(0.05)
    assert line.sent, "agent audio never reached the caller after the callee spoke"

    script.tool_calls("log_contact_attempt", "end_call")
    await asyncio.wait_for(task, 3)
    assert line.closed
    first_after_setup = next(f for f in sock.sent if not script.is_setup(f))
    assert script.is_prime(first_after_setup), (
        "the first request after setup must be the priming turn, not a greeting"
    )


# --- pipecat: what the LLM receives ------------------------------------------------------


def _completion(names: list[str]) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": f"call_{n}", "type": "function", "function": {"name": n, "arguments": "{}"}}
                        for n in names
                    ],
                },
            }
        ],
    }


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_primes_with_traced_sdk_calls_and_sends_the_same_context() -> None:
    from openai import AsyncOpenAI

    from remi_core.providers.base import TurnTaking
    from vendors.pipecat import PipecatProvider, user_speech_timeout_s, vad_params
    from vendors.pipecat.services import ServiceConfig

    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        names = list(CONNECT_READS[:2]) if len(requests) == 1 else [body["tool_choice"]["function"]["name"]]
        return httpx.Response(200, json=_completion(names))

    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )
    provider.openai_client = lambda: AsyncOpenAI(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    session = ScriptedSession()
    call = runtime(session=session)
    context = provider.build_context(call.setup)
    await provider.prime(call, context)

    # Priming: forced tool calls through the instrumented SDK, the missing read forced by name.
    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["tool_choice"] == {"type": "function", "function": {"name": "get_contact_history"}}
    assert requests[0]["messages"][0]["content"] == setup().instructions
    assert requests[0]["messages"][1]["content"] == PRIME_INSTRUCTION
    assert {
        t["function"]["name"]: (
            set(t["function"]["parameters"]["properties"]),
            set(t["function"]["parameters"]["required"]),
        )
        for t in requests[0]["tools"]
    } == EXPECTED
    assert [c for c, _ in session.calls] == list(CONNECT_READS)

    # What the pipeline's first real completion will send: the same instructions and tools, the reads already in context.
    llm = provider.build_llm(call.setup)
    params = llm.get_llm_adapter().get_llm_invocation_params(
        context, system_instruction=llm._settings.system_instruction, convert_developer_to_user=False
    )
    assert params["messages"][0] == {"role": "system", "content": setup().instructions}
    assert [m["role"] for m in params["messages"][1:]] == ["assistant", "tool", "tool", "assistant", "tool"]
    assert {t["function"]["name"] for t in params["tools"]} == set(EXPECTED)
    assert not any(m["role"] == "assistant" and m.get("content") for m in params["messages"]), (
        "no greeting before the callee"
    )

    turns = TurnTaking()
    assert vad_params(turns).stop_secs + user_speech_timeout_s(turns) == pytest.approx(
        turns.silence_ms / 1000
    )


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_pipeline_assembles_against_the_pinned_pipecat() -> None:
    from unittest.mock import MagicMock

    from vendors.pipecat import PipecatProvider
    from vendors.pipecat.services import ServiceConfig

    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai", deepgram_api_key="dg"),
        "gpt-test",
    )
    call = runtime()
    call.line.websocket = MagicMock()
    worker = provider.build_worker(call, provider.build_context(call.setup))
    assert type(worker).__name__ == "PipelineWorker"
