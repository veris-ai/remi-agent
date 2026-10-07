"""Parity: every installed vendor sends the same agent, for every agent (agents x vendors).

Each case runs a provider through a whole scripted call offline and asserts on
what it actually SENT (the vendor's session/setup frame, its priming request,
its audio toward the caller), not on module attributes:

- byte-identical instructions, and exactly the registry's tools and schemas;
- the same turn-taking numbers mapped into each vendor's VAD, as far as the
  vendor exposes one (each script declares what its vendor maps: Grok has no
  prefix padding, GPT-Live has no VAD at all);
- CALLEE_FIRST: the connect reads run first, the caller's "Hello?" is held
  until they finish, no greeting is triggered, and no agent audio reaches the
  caller before the callee has spoken;
- AGENT_FIRST (the test-only inbound agent): the opening turn is the first
  request after setup, caller audio is never held, and agent audio reaches
  the caller before they speak;
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

from vamos_voice.providers import load_provider, vendor_names

from .fakes import AGENT_CASES, HELLO, AgentCase, FakeLine, FakeVendorSocket, ScriptedSession, runtime

CASES = {case.name: case for case in AGENT_CASES}


def installed(name: str) -> bool:
    try:
        load_provider(name)
    except ImportError:
        return False
    return True


def test_every_vendor_folder_is_covered_here() -> None:
    assert set(vendor_names()) == {"openai_realtime", "gemini_live", "grok_voice", "gpt_live", "pipecat"}


def test_every_agent_pack_is_covered_here() -> None:
    from vamos_voice.registry import agent_names

    assert set(agent_names()) <= set(CASES), "add the new agent to tests/fakes.AGENT_CASES"


@pytest.mark.parametrize("name", vendor_names())
def test_provider_metadata(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    provider = load_provider(name)
    assert provider.name == name
    assert provider.trace_adapter in {None, "openai-realtime", "gemini-live", "gpt-live"}


# --- realtime-protocol vendors: a scripted vendor socket --------------------------------------


def openai_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.openai_realtime import OpenAIRealtimeProvider

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
                    "arguments": "{}",
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

    #: (silence_ms, prefix_padding_ms) the vendor is sent; None where it has no such setting.
    expected_vad = (800, 300)

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
    def check_prime(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["response"]["tool_choice"] == "required"

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["type"] == "response.create" and "tool_choice" not in frame["response"]
        setup = case.setup()
        assert frame["response"]["instructions"] == (
            f"{setup.instructions}\n\n{case.policy.opening_instruction}"
        )

    def ready(self) -> None:
        pass


class GrokScript(OpenAIScript):
    """Grok's Realtime dialect: top-level VAD, grok-transcribe, priming by a labelled note."""

    expected_vad = (800, None)

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        session = frame["session"]
        vad = session["turn_detection"]
        return {
            "instructions": session["instructions"],
            "tools": {
                t["name"]: (set(t["parameters"]["properties"]), set(t["parameters"]["required"]))
                for t in session["tools"]
            },
            "silence_ms": vad["silence_duration_ms"],
            "prefix_ms": vad.get("prefix_padding_ms"),
            "transcribes_caller": session["audio"]["input"]["transcription"] == {"model": "grok-transcribe"},
        }

    # Grok cannot force a tool call: priming is a user-role note item, then response.create.
    is_prime = staticmethod(
        lambda f: (
            (f.get("type") == "conversation.item.create" and f["item"]["type"] == "message")
            or (f.get("type") == "response.create" and "instructions" in f.get("response", {}))
        )
    )

    @staticmethod
    def check_prime(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["item"]["role"] == "user"
        assert frame["item"]["content"] == [{"type": "input_text", "text": case.policy.prime_note}]


def grok_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.grok_voice import GrokVoiceProvider

    provider = GrokVoiceProvider("xai-test", "grok-voice-think-fast-2.0", "eve")
    provider.connect = sock.connector()
    return {"provider": provider}


class GptLiveScript:
    """Server events in the GPT-Live protocol: tool calls arrive from the delegated backend."""

    expected_vad = (None, None)  # full duplex: GPT-Live exposes no VAD settings

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        self.sock.push({"type": "session.started", "session": {"id": "sess_1", "model": "gpt-live-1"}})

    def tool_calls(self, *names: str) -> None:
        self.n += 1
        delegation = f"item_d{self.n}"
        self.sock.push(
            {"type": "session.delegation.created", "delegation": {"id": delegation, "target": "responses"}}
        )
        for i, name in enumerate(names):
            item = {"type": "function_call", "call_id": f"call_{self.n}_{i}", "name": name, "arguments": "{}"}
            self.sock.push(
                {
                    "type": "response.event",
                    "delegation_id": delegation,
                    "event": {"type": "response.output_item.done", "item": item},
                }
            )
        self.sock.push(
            {"type": "response.event", "delegation_id": delegation, "event": {"type": "response.completed"}}
        )

    def audio(self) -> None:
        self.sock.push(
            {"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x00\x01" * 240).decode()}
        )

    def callee_spoke(self) -> None:
        self.sock.push(
            {"type": "session.input_transcript.delta", "delta": "Hello?", "start_ms": 0, "end_ms": 400}
        )

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        session = frame["session"]
        backend = session["delegation"]["responses"]
        assert backend["instructions"].endswith(session["instructions"])
        return {
            "instructions": session["instructions"],
            "tools": {
                t["name"]: (set(t["parameters"]["properties"]), set(t["parameters"]["required"]))
                for t in backend["tools"]
            },
            "silence_ms": None,
            "prefix_ms": None,
            "transcribes_caller": True,  # the session always streams the caller's transcript
        }

    is_setup = staticmethod(lambda f: f.get("type") == "session.start")
    is_prime = staticmethod(lambda f: f.get("type") == "session.instructions.append")
    is_caller_audio = staticmethod(lambda f: f.get("type") == "session.input_audio.append")

    @staticmethod
    def check_prime(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame == {
            "type": "session.instructions.append",
            "delegation_id": None,
            "content": case.policy.prime_instruction,
        }

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame == {
            "type": "session.instructions.append",
            "delegation_id": None,
            "content": case.policy.opening_instruction,
        }


def gpt_live_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.gpt_live import GptLiveProvider

    provider = GptLiveProvider("sk-test", "gpt-live-1", "gpt-5.6-terra", "marin")
    provider.connect = sock.connector()
    return {"provider": provider}


class GeminiScript:
    """Server messages in the Gemini Live protocol."""

    expected_vad = (800, 300)

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        self.sock.push({"setupComplete": {}})

    def tool_calls(self, *names: str) -> None:
        calls = [
            {
                "id": f"fc_{self.n}_{i}",
                "name": n,
                "args": {},
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
    def check_prime(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["clientContent"]["turns"][0]["parts"][0]["text"] == case.policy.prime_note

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["clientContent"]["turns"][0]["parts"][0]["text"] == case.policy.opening_note


def gemini_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.gemini_live import GeminiLiveProvider

    provider = GeminiLiveProvider("test-key", "gemini-test", "Puck")
    provider.connect = sock.connector()
    return {"provider": provider}


SOCKET_VENDORS = {
    "openai_realtime": (openai_call, OpenAIScript),
    "gemini_live": (gemini_call, GeminiScript),
    "grok_voice": (grok_call, GrokScript),
    "gpt_live": (gpt_live_call, GptLiveScript),
}


CALLEE_FIRST_CASES = sorted(n for n, c in CASES.items() if not c.policy.agent_opens)
AGENT_FIRST_CASES = sorted(n for n, c in CASES.items() if c.policy.agent_opens)


def test_the_matrix_covers_both_call_starts() -> None:
    assert CALLEE_FIRST_CASES and AGENT_FIRST_CASES


@pytest.mark.parametrize("agent", CALLEE_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_primes_and_waits_for_the_callee(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    case = CASES[agent]
    reads = case.policy.connect_reads
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line, session = FakeLine(), ScriptedSession(reads)
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    sent_setup = script.setup(await sock.wait_for(script.is_setup))
    assert sent_setup["instructions"] == case.setup().instructions
    assert sent_setup["tools"] == case.expected_tools()
    assert (sent_setup["silence_ms"], sent_setup["prefix_ms"]) == script.expected_vad
    assert sent_setup["transcribes_caller"]
    assert "x-goog-api-key" in sock.headers or "Authorization" in sock.headers
    assert "key=" not in sock.url

    script.ready()
    await line.incoming.put(HELLO)  # the callee answers while priming runs
    script.check_prime(await sock.wait_for(script.is_prime), case)
    await asyncio.sleep(0.05)
    assert not any(script.is_caller_audio(f) for f in sock.sent), (
        "caller audio reached the vendor before priming finished"
    )

    script.tool_calls(*reads)
    await sock.wait_for(script.is_caller_audio)  # the held "Hello?" is released after the reads
    assert [c for c, _ in session.calls] == list(reads)
    assert call.start.primed

    script.audio()  # the model talks before the callee has been heard
    await asyncio.sleep(0.05)
    assert line.sent == [], "agent audio reached the caller before the callee spoke"
    script.callee_spoke()
    script.audio()
    await asyncio.sleep(0.05)
    assert line.sent, "agent audio never reached the caller after the callee spoke"

    script.tool_calls("end_call")
    await asyncio.wait_for(task, 3)
    assert line.closed
    first_after_setup = next(f for f in sock.sent if not script.is_setup(f))
    assert script.is_prime(first_after_setup), (
        "the first request after setup must be the priming turn, not a greeting"
    )


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_lets_an_inbound_agent_speak_first(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    case = CASES[agent]
    assert not case.policy.connect_reads, "this test covers an opening with no connect reads"
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line, session = FakeLine(), ScriptedSession(())
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    sent_setup = script.setup(await sock.wait_for(script.is_setup))
    assert sent_setup["instructions"] == case.setup().instructions
    assert sent_setup["tools"] == case.expected_tools()
    assert (sent_setup["silence_ms"], sent_setup["prefix_ms"]) == script.expected_vad

    script.ready()
    script.check_opening(await sock.wait_for(script.is_prime), case)  # the opening turn is requested
    assert call.start.primed and call.start.audio_open

    script.audio()  # the greeting, before the caller has said anything
    await asyncio.sleep(0.05)
    assert line.sent, "an inbound agent's greeting must reach the caller before they speak"

    await line.incoming.put(HELLO)  # caller audio is never held for an inbound agent
    await sock.wait_for(script.is_caller_audio)

    script.tool_calls("verify_caller")
    script.tool_calls("end_call")
    await asyncio.wait_for(task, 3)
    assert line.closed
    assert [c for c, _ in session.calls] == ["verify_caller", "end_call"]
    first_after_setup = next(f for f in sock.sent if not script.is_setup(f))
    script.check_opening(first_after_setup, case)


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


@pytest.mark.parametrize("agent", CALLEE_FIRST_CASES)
@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_primes_with_traced_sdk_calls_and_sends_the_same_context(agent: str) -> None:
    from openai import AsyncOpenAI

    from vamos_voice.providers.base import TurnTaking
    from vamos_voice.providers.pipecat import PipecatProvider, user_speech_timeout_s, vad_params
    from vamos_voice.providers.pipecat.services import ServiceConfig

    case = CASES[agent]
    reads = case.policy.connect_reads
    expected = case.expected_tools()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        names = list(reads[:-1]) if len(requests) == 1 else [body["tool_choice"]["function"]["name"]]
        return httpx.Response(200, json=_completion(names))

    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )
    provider.openai_client = lambda: AsyncOpenAI(
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    session = ScriptedSession(reads)
    call = runtime(case, session=session)
    context = provider.build_context(call.setup)
    await provider.prime(call, context)

    # Priming: forced tool calls through the instrumented SDK, the missing read forced by name.
    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["tool_choice"] == {"type": "function", "function": {"name": reads[-1]}}
    assert requests[0]["messages"][0]["content"] == case.setup().instructions
    assert requests[0]["messages"][1]["content"] == case.policy.prime_instruction
    assert {
        t["function"]["name"]: (
            set(t["function"]["parameters"]["properties"]),
            set(t["function"]["parameters"]["required"]),
        )
        for t in requests[0]["tools"]
    } == expected
    assert [c for c, _ in session.calls] == list(reads)

    # What the pipeline's first real completion will send: the same instructions and tools, the reads already in context.
    llm = provider.build_llm(call.setup)
    params = llm.get_llm_adapter().get_llm_invocation_params(
        context, system_instruction=llm._settings.system_instruction, convert_developer_to_user=False
    )
    assert params["messages"][0] == {"role": "system", "content": case.setup().instructions}
    roles = [m["role"] for m in params["messages"][1:]]
    assert roles == ["assistant", *["tool"] * (len(reads) - 1), "assistant", "tool"]
    assert {t["function"]["name"] for t in params["tools"]} == set(expected)
    assert not any(m["role"] == "assistant" and m.get("content") for m in params["messages"]), (
        "no greeting before the callee"
    )

    turns = TurnTaking()
    assert vad_params(turns).stop_secs + user_speech_timeout_s(turns) == pytest.approx(
        turns.silence_ms / 1000
    )


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_opens_the_call_for_an_inbound_agent(agent: str) -> None:
    from pipecat.frames.frames import LLMMessagesAppendFrame

    from vamos_voice.providers.pipecat import PipecatProvider
    from vamos_voice.providers.pipecat.services import ServiceConfig

    case = CASES[agent]
    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )

    def no_llm() -> Any:
        raise AssertionError("an agent with no connect reads must not prime")

    provider.openai_client = no_llm
    call = runtime(case, session=ScriptedSession(()))
    context = provider.build_context(call.setup)
    await provider.prime(call, context)
    assert context.get_messages() == []
    (frame,) = provider.opening_frames(call.setup)
    assert isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm
    assert frame.messages == [{"role": "system", "content": case.policy.opening_instruction}]
    assert provider.opening_frames(CASES["remi"].setup()) == [], "a callee-first agent never opens"


@pytest.mark.parametrize("agent", sorted(CASES))
@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_pipeline_assembles_against_the_pinned_pipecat(agent: str) -> None:
    from unittest.mock import MagicMock

    from vamos_voice.providers.pipecat import PipecatProvider
    from vamos_voice.providers.pipecat.services import ServiceConfig

    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai", deepgram_api_key="dg"),
        "gpt-test",
    )
    call = runtime(CASES[agent])
    call.line.websocket = MagicMock()
    worker = provider.build_worker(call, provider.build_context(call.setup))
    assert type(worker).__name__ == "PipelineWorker"
