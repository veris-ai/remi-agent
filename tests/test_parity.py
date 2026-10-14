"""Parity: every installed vendor sends the same agent, for every agent (agents x vendors).

Each case runs a provider through a whole scripted call offline and asserts on
what it actually SENT (the vendor's session/setup frame, its requests, its
audio toward the caller), not on module attributes:

- byte-identical instructions, and exactly the registry's tools and schemas;
- the same turn-taking numbers mapped into each vendor's VAD, as far as the
  vendor exposes one (each script declares what its vendor maps: Grok has no
  prefix padding, GPT-Live has no VAD at all);
- the screen pop: Remi's connect reads run (as SQL through the agent's own
  database client) before the vendor session opens, so before any caller
  audio is forwarded and any agent audio is sent; the setup carries the
  "Account on screen" section without the verification secrets; nothing
  forces a tool call; the gates treat the reads as done;
- CALLEE_FIRST: nothing asks the model to act, the caller's "Hello?" goes
  straight to the vendor, and no agent audio reaches the caller before the
  callee has spoken;
- AGENT_FIRST (the test-only inbound agent): the opening turn is the first
  request after setup, and agent audio reaches the caller before they speak;
- end_call hangs up the line.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
from typing import Any

import pytest

from vamos_voice.providers import load_provider, vendor_names
from vamos_voice.providers.gpt_live import BACKEND_PREAMBLE, LIVE_PREAMBLE

from .fakes import (
    AGENT_CASES,
    HELLO,
    REMI_SECRETS,
    AgentCase,
    FakeLine,
    FakeVendorSocket,
    ScriptedSession,
    remi_screen_pop_runtime,
    runtime,
)

CASES = {case.name: case for case in AGENT_CASES}


def installed(name: str) -> bool:
    try:
        load_provider(name)
    except ImportError:
        return False
    return True


def test_every_vendor_folder_is_covered_here() -> None:
    assert set(vendor_names()) == {
        "openai_realtime",
        "gemini_live",
        "grok_voice",
        "gpt_live",
        "pipecat",
        "deepgram",
        "elevenlabs",
        "fluxions",
        "mistral",
        "huggingface",
        "grok_cascade",
        "gradium",
        "vapi",
        "cartesia",
        "livekit",
    }


def test_every_agent_pack_is_covered_here() -> None:
    from vamos_voice.registry import agent_names

    assert set(agent_names()) <= set(CASES), "add the new agent to tests/fakes.AGENT_CASES"


#: Vendor keys use their standard names (bench candidate-env secrets, as on the other VAmoS benches).
STANDARD_KEYS = {
    "openai_realtime": ("OPENAI_API_KEY",),
    "gpt_live": ("OPENAI_API_KEY",),
    "gemini_live": ("GEMINI_API_KEY",),
    "grok_voice": ("XAI_API_KEY",),
    "deepgram": ("DEEPGRAM_API_KEY",),
    "elevenlabs": ("ELEVENLABS_API_KEY",),
    "fluxions": ("FLUXIONS_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "huggingface": ("HF_TOKEN",),
    "grok_cascade": ("XAI_API_KEY",),  # plus OPENAI_API_KEY for its default LLM (checked in from_env)
    "gradium": ("GRADIUM_API_KEY", "OPENAI_API_KEY"),
}


@pytest.mark.parametrize("name", sorted(STANDARD_KEYS))
def test_speech_to_speech_vendors_read_the_standard_key_names(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    assert load_provider(name).required_env == STANDARD_KEYS[name]


@pytest.mark.parametrize("name", vendor_names())
def test_provider_metadata(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    provider = load_provider(name)
    assert provider.name == name
    assert provider.trace_adapter in {
        None,
        "openai-realtime",
        "gemini-live",
        "gpt-live",
        "deepgram-voice-agent",
        "elevenlabs-convai",
        "fluxions-realtime",
        "gradbot",
        "vapi",
        "cartesia",
    }


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
        self.sock.push({"type": "response.created", "response": {"id": f"r{self.n}"}})
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
    #: A frame asking the model to act (a response, or a turn written into the conversation).
    is_request = staticmethod(lambda f: f.get("type") in ("response.create", "conversation.item.create"))
    is_caller_audio = staticmethod(lambda f: f.get("type") == "input_audio_buffer.append")

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
    """Grok's Realtime dialect: top-level VAD, grok-transcribe."""

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
        assert backend["instructions"].startswith(BACKEND_PREAMBLE)
        instructions = backend["instructions"][len(BACKEND_PREAMBLE) :]
        # the live model gets the same instructions after the list of the backend's actions
        assert session["instructions"].startswith(LIVE_PREAMBLE)
        assert session["instructions"].endswith(instructions)
        return {
            "instructions": instructions,
            "tools": {
                t["name"]: (set(t["parameters"]["properties"]), set(t["parameters"]["required"]))
                for t in backend["tools"]
            },
            "silence_ms": None,
            "prefix_ms": None,
            "transcribes_caller": True,  # the session always streams the caller's transcript
        }

    is_setup = staticmethod(lambda f: f.get("type") == "session.start")
    is_request = staticmethod(
        lambda f: f.get("type") in ("session.instructions.append", "session.update", "response.create")
    )
    is_caller_audio = staticmethod(lambda f: f.get("type") == "session.input_audio.append")

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
    is_request = staticmethod(lambda f: "clientContent" in f)
    is_caller_audio = staticmethod(lambda f: "realtimeInput" in f)

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["clientContent"]["turns"][0]["parts"][0]["text"] == case.policy.opening_note


def gemini_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.gemini_live import GeminiLiveProvider

    provider = GeminiLiveProvider("test-key", "gemini-test", "Puck")
    provider.connect = sock.connector()
    return {"provider": provider}


AGENT_AUDIO = b"\x00\x01" * 240


def _flat_tools(tools: list[dict[str, Any]]) -> dict[str, tuple[set[str], set[str]]]:
    return {t["name"]: (set(t["parameters"]["properties"]), set(t["parameters"]["required"])) for t in tools}


class DeepgramScript:
    """Server events in the Deepgram Voice Agent protocol: JSON text frames, binary audio."""

    expected_vad = (None, None)  # nova-3 exposes no endpointing setting in Settings

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        self.sock.push({"type": "Welcome", "request_id": "req_1"})
        self.sock.push({"type": "SettingsApplied"})

    def tool_calls(self, *names: str) -> None:
        self.n += 1
        functions = [
            {"id": f"fn_{self.n}_{i}", "name": n, "arguments": "{}", "client_side": True}
            for i, n in enumerate(names)
        ]
        self.sock.push({"type": "FunctionCallRequest", "functions": functions})
        self.sock.push({"type": "AgentAudioDone"})

    def audio(self) -> None:
        self.sock.push_binary(AGENT_AUDIO)

    def callee_spoke(self) -> None:
        self.sock.push({"type": "ConversationText", "role": "user", "content": "Hello?"})

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        think = frame["agent"]["think"]
        assert "greeting" not in frame["agent"], "Deepgram would speak a greeting before anyone asked"
        assert frame["audio"]["input"]["sample_rate"] == frame["audio"]["output"]["sample_rate"] == 24_000
        return {
            "instructions": think["prompt"],
            "tools": _flat_tools(think["functions"]),
            "silence_ms": None,
            "prefix_ms": None,
            "transcribes_caller": bool(frame["agent"]["listen"]["provider"]["model"]),
        }

    is_setup = staticmethod(lambda f: f.get("type") == "Settings")
    is_request = staticmethod(lambda f: f.get("type") in ("InjectUserMessage", "InjectAgentMessage"))
    is_caller_audio = staticmethod(lambda f: "binary" in f)

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame == {"type": "InjectUserMessage", "content": case.policy.opening_note}


def deepgram_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.deepgram import DeepgramProvider

    provider = DeepgramProvider("dg-test", "nova-3", "gpt-4.1-mini", "aura-2-thalia-en")
    provider.connect = sock.connector()
    return {"provider": provider}


class FakeAgentStore:
    """ElevenLabs' agents API: records each per-call agent on the socket (``sock.agents``)."""

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock = sock
        sock.agents, sock.deleted = [], []

    async def create(self, body: dict[str, Any]) -> str:
        self.sock.agents.append(body)
        return f"agent_{len(self.sock.agents)}"

    async def delete(self, agent_id: str) -> None:
        self.sock.deleted.append(agent_id)


class ElevenLabsScript:
    """Server events in the ElevenLabs ConvAI protocol; the tools live on the per-call agent."""

    expected_vad = (None, None)  # a learned turn model: no silence threshold to map

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        self.sock.push(
            {
                "type": "conversation_initiation_metadata",
                "conversation_initiation_metadata_event": {
                    "conversation_id": "conv_1",
                    "agent_output_audio_format": "pcm_24000",
                    "user_input_audio_format": "pcm_24000",
                },
            }
        )

    def tool_calls(self, *names: str) -> None:
        for i, name in enumerate(names):
            self.n += 1
            self.sock.push(
                {
                    "type": "client_tool_call",
                    "client_tool_call": {
                        "tool_name": name,
                        "tool_call_id": f"tc_{self.n}_{i}",
                        "parameters": {},
                    },
                }
            )

    def audio(self) -> None:
        self.sock.push(
            {
                "type": "audio",
                "audio_event": {"audio_base_64": base64.b64encode(AGENT_AUDIO).decode(), "event_id": 1},
            }
        )

    def callee_spoke(self) -> None:
        self.sock.push({"type": "user_transcript", "user_transcription_event": {"user_transcript": "Hello?"}})

    def setup(self, frame: dict[str, Any]) -> dict[str, Any]:
        (agent,) = self.sock.agents
        config = agent["conversation_config"]
        prompt = config["agent"]["prompt"]
        override = frame["conversation_config_override"]["agent"]
        assert override["prompt"]["prompt"] == prompt["prompt"], "the trace's prompt is the agent's"
        assert config["agent"]["first_message"] == override["first_message"] == ""
        assert all(t["type"] == "client" for t in prompt["tools"])
        return {
            "instructions": prompt["prompt"],
            "tools": _flat_tools(prompt["tools"]),
            "silence_ms": None,
            "prefix_ms": None,
            "transcribes_caller": True,  # ASR is the platform's; user_transcript always streams
        }

    is_setup = staticmethod(lambda f: f.get("type") == "conversation_initiation_client_data")
    is_request = staticmethod(lambda f: f.get("type") in ("user_message", "contextual_update"))
    is_caller_audio = staticmethod(lambda f: "user_audio_chunk" in f)

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame == {"type": "user_message", "text": case.policy.opening_note}


def elevenlabs_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.elevenlabs import ElevenLabsProvider

    provider = ElevenLabsProvider("el-test", "gpt-4.1-mini", "eleven_flash_v2", "voice")
    provider.connect = sock.connector()
    provider.agents = FakeAgentStore(sock)
    return {"provider": provider}


class FluxionsScript:
    """Server events in the Fluxions realtime protocol: JSON text frames, binary audio."""

    expected_vad = (None, None)  # an adaptive end-of-utterance model with no knob

    def __init__(self, sock: FakeVendorSocket) -> None:
        self.sock, self.n = sock, 0

    def ready(self) -> None:
        update = next(f for f in self.sock.sent if f.get("type") == "session.update")
        self.sock.push(
            {
                "type": "session.created",
                "session_id": "s1",
                "model": "vllm:test",
                "mic_sample_rate": 16_000,
                "audio_sample_rate": 24_000,
                "soul": update["soul"],
                "client_tools": [t["name"] for t in update["tools"]],
                "client_tools_rejected": [],
                "denied": [],
            }
        )

    def tool_calls(self, *names: str) -> None:
        for i, name in enumerate(names):
            self.n += 1
            self.sock.push(
                {"type": "tool.call", "call_id": f"call_{self.n}_{i}", "name": name, "arguments": {}}
            )
        self.sock.push({"type": "agent.done", "cancelled": False})

    def audio(self) -> None:
        self.sock.push_binary(AGENT_AUDIO)

    def callee_spoke(self) -> None:
        self.sock.push({"type": "user.transcript", "text": "Hello?"})

    @staticmethod
    def setup(frame: dict[str, Any]) -> dict[str, Any]:
        assert frame["allow_hangup"] is False
        return {
            "instructions": frame["soul"],
            "tools": _flat_tools(frame["tools"]),
            "silence_ms": None,
            "prefix_ms": None,
            "transcribes_caller": True,  # user.transcript always streams
        }

    is_setup = staticmethod(lambda f: f.get("type") == "session.update")
    #: The opening is Fluxions' own greet turn, asked for in the setup (no turn request exists).
    is_request = staticmethod(lambda f: f.get("type") == "session.update" and f["greet"])
    is_caller_audio = staticmethod(lambda f: "binary" in f)
    opens_in_setup = True

    @staticmethod
    def check_opening(frame: dict[str, Any], case: AgentCase) -> None:
        assert frame["type"] == "session.update" and frame["greet"] is True


def fluxions_call(sock: FakeVendorSocket) -> dict[str, Any]:
    from vamos_voice.providers.fluxions import FluxionsProvider

    provider = FluxionsProvider("fx-test", "https://fluxions.test/vui", "maeve")
    provider.connect = sock.connector()
    return {"provider": provider}


SOCKET_VENDORS = {
    "openai_realtime": (openai_call, OpenAIScript),
    "gemini_live": (gemini_call, GeminiScript),
    "grok_voice": (grok_call, GrokScript),
    "gpt_live": (gpt_live_call, GptLiveScript),
    "deepgram": (deepgram_call, DeepgramScript),
    "elevenlabs": (elevenlabs_call, ElevenLabsScript),
    "fluxions": (fluxions_call, FluxionsScript),
}


CALLEE_FIRST_CASES = sorted(n for n, c in CASES.items() if not c.policy.agent_opens)
AGENT_FIRST_CASES = sorted(n for n, c in CASES.items() if c.policy.agent_opens)


def test_the_matrix_covers_both_call_starts() -> None:
    assert CALLEE_FIRST_CASES and AGENT_FIRST_CASES


@pytest.mark.parametrize("agent", CALLEE_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_waits_for_the_callee(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    case = CASES[agent]
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    sent_setup = script.setup(await sock.wait_for(script.is_setup))
    assert sent_setup["instructions"] == case.setup().instructions
    assert sent_setup["tools"] == case.expected_tools()
    assert (sent_setup["silence_ms"], sent_setup["prefix_ms"]) == script.expected_vad
    assert sent_setup["transcribes_caller"]
    assert {"x-goog-api-key", "Authorization", "xi-api-key"} & set(sock.headers)
    assert "key=" not in sock.url

    script.ready()
    await line.incoming.put(HELLO)  # the callee answers
    await sock.wait_for(script.is_caller_audio)  # straight to the vendor: nothing is held
    assert not any(script.is_request(f) for f in sock.sent), "nothing asks the model to act first"

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
    assert [c for c, _ in session.calls] == ["end_call"]


def _forced_tool_choices(node: Any) -> list[Any]:
    """Every ``tool_choice`` in a frame other than ``auto`` (a forced or required tool call)."""
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "tool_choice" and v != "auto"]
        return found + [x for v in node.values() for x in _forced_tool_choices(v)]
    if isinstance(node, list):
        return [x for v in node for x in _forced_tool_choices(v)]
    return []


@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_starts_with_remis_screen_pop(name: str) -> None:
    """GPT-Live never ran the reads when priming asked for them, and asked for the agency's callback
    contact instead of the consumer: the screen pop gives every vendor the account before the call."""
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    events: list[str] = []
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket(events)
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line = FakeLine(events)
    await line.incoming.put(HELLO)  # the callee is already talking when the call connects
    call = await remi_screen_pop_runtime(events, line)
    task = asyncio.create_task(provider.run_call(call))

    instructions = script.setup(await sock.wait_for(script.is_setup))["instructions"]
    assert instructions == call.setup.instructions
    assert instructions.startswith(CASES["remi"].setup().instructions.rstrip())
    pop = instructions.split("# Account on screen", 1)[1]
    assert "You are calling Marcus Sokolov" in pop and "Call assessment: the call may proceed." in pop
    for read in ("get_account", "get_dialed_phone", "get_contact_history"):
        assert f"\n{read}: {{" in pop
    for secret in REMI_SECRETS:
        assert secret not in instructions, f"verification secret {secret!r} reached the instructions"

    script.ready()
    await sock.wait_for(script.is_caller_audio)
    script.callee_spoke()
    script.audio()
    await asyncio.sleep(0.05)
    reads = [i for i, e in enumerate(events) if e.startswith("sql:")]
    vendor = [i for i, e in enumerate(events) if e.startswith("vendor:")]
    assert {events[i] for i in reads} >= {"sql:accounts", "sql:phones", "sql:contact_attempts"}
    assert max(reads) < min(vendor) < events.index("agent audio"), events
    assert not any(script.is_request(f) for f in sock.sent), "no priming request of any kind"
    assert not [c for f in sock.sent for c in _forced_tool_choices(f)], "no forced tool choice"

    script.tool_calls("take_payment")  # the gates treat the reads as done: verification is next
    await asyncio.sleep(0.05)
    ((tool, refusal),) = call.session.results  # type: ignore[attr-defined]
    assert tool == "take_payment" and refusal["error"].startswith("Not verified yet")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(SOCKET_VENDORS))
async def test_socket_vendor_lets_an_inbound_agent_speak_first(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    case = CASES[agent]
    build, script_cls = SOCKET_VENDORS[name]
    sock = FakeVendorSocket()
    script = script_cls(sock)
    provider = build(sock)["provider"]
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    sent_setup = script.setup(await sock.wait_for(script.is_setup))
    assert sent_setup["instructions"] == case.setup().instructions
    assert sent_setup["tools"] == case.expected_tools()
    assert (sent_setup["silence_ms"], sent_setup["prefix_ms"]) == script.expected_vad

    script.ready()
    script.check_opening(await sock.wait_for(script.is_request), case)  # the opening turn is requested
    assert call.start.audio_open

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
    if not getattr(script, "opens_in_setup", False):
        first_after_setup = next(f for f in sock.sent if not script.is_setup(f))
        script.check_opening(first_after_setup, case)


# --- pipecat: what the LLM receives ------------------------------------------------------


@pytest.mark.parametrize("agent", sorted(CASES))
@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_sends_the_same_instructions_and_tools(agent: str) -> None:
    from vamos_voice.providers.base import TurnTaking
    from vamos_voice.providers.pipecat import PipecatProvider, user_speech_timeout_s, vad_params
    from vamos_voice.providers.pipecat.services import ServiceConfig

    case = CASES[agent]
    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )
    call = runtime(case)
    params = _pipecat_request(provider, call)
    assert params["messages"] == [{"role": "system", "content": case.setup().instructions}]
    assert {
        t["function"]["name"]: (
            set(t["function"]["parameters"]["properties"]),
            set(t["function"]["parameters"]["required"]),
        )
        for t in params["tools"]
    } == case.expected_tools()

    turns = TurnTaking()
    assert vad_params(turns).stop_secs + user_speech_timeout_s(turns) == pytest.approx(
        turns.silence_ms / 1000
    )


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_runs_its_own_model_default_and_turn_start() -> None:
    from pipecat.turns.user_turn_strategies import default_user_turn_start_strategies

    from vamos_voice.providers.base import TurnTaking
    from vamos_voice.providers.pipecat import PipecatProvider, turn_strategies

    assert (
        PipecatProvider.from_env({"OPENAI_API_KEY": "k", "DEEPGRAM_API_KEY": "d"}).llm_model == "gpt-4.1-mini"
    )
    start = turn_strategies(TurnTaking()).start or []
    assert [type(s) for s in start] == [type(s) for s in default_user_turn_start_strategies()]


def _pipecat_request(provider: Any, call: Any) -> dict[str, Any]:
    """What the pipeline's first completion sends: the system instruction plus the context."""
    llm = provider.build_llm(call.setup)
    return llm.get_llm_adapter().get_llm_invocation_params(
        provider.build_context(call.setup),
        system_instruction=llm._settings.system_instruction,
        convert_developer_to_user=False,
    )


@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
async def test_pipecat_starts_with_remis_screen_pop_and_an_empty_context() -> None:
    from vamos_voice.providers.pipecat import PipecatProvider
    from vamos_voice.providers.pipecat.services import ServiceConfig

    events: list[str] = []
    call = await remi_screen_pop_runtime(events, FakeLine(events))
    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )
    (system,) = _pipecat_request(provider, call)["messages"]  # no forced read in the context
    assert system["role"] == "system" and "# Account on screen" in system["content"]
    assert "You are calling Marcus Sokolov" in system["content"]
    assert not any(secret in system["content"] for secret in REMI_SECRETS)
    assert {"sql:accounts", "sql:phones", "sql:contact_attempts"} <= set(events)


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
@pytest.mark.skipif(importlib.util.find_spec("pipecat") is None, reason="pipecat extra not installed")
def test_pipecat_opens_the_call_for_an_inbound_agent(agent: str) -> None:
    from pipecat.frames.frames import LLMMessagesAppendFrame

    from vamos_voice.providers.pipecat import PipecatProvider
    from vamos_voice.providers.pipecat.services import ServiceConfig

    case = CASES[agent]
    provider = PipecatProvider(
        ServiceConfig(openai_api_key="sk-test", stt="deepgram", tts="openai"), "gpt-test"
    )
    call = runtime(case)
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


# --- in-pod cascades: what the chat model receives, through the shared cascade ------------------


def mistral_provider() -> Any:
    from vamos_voice.providers.mistral import MistralConfig, MistralProvider

    return MistralProvider(MistralConfig(api_key="mistral-test"))


def huggingface_provider() -> Any:
    from vamos_voice.providers.huggingface import HuggingFaceConfig, HuggingFaceProvider

    return HuggingFaceProvider(HuggingFaceConfig(token="hf-test"))


def grok_cascade_provider() -> Any:
    from vamos_voice.providers.grok_cascade import GrokCascadeConfig, GrokCascadeProvider

    return GrokCascadeProvider(GrokCascadeConfig(xai_api_key="xai-test", llm_api_key="sk-test"))


CASCADE_VENDORS = {
    "mistral": mistral_provider,
    "huggingface": huggingface_provider,
    "grok_cascade": grok_cascade_provider,
}


def _tools_of(request_tools: list[dict[str, Any]]) -> dict[str, tuple[set[str], set[str]]]:
    return {
        t["function"]["name"]: (
            set(t["function"]["parameters"]["properties"]),
            set(t["function"]["parameters"]["required"]),
        )
        for t in request_tools
    }


@pytest.mark.parametrize("agent", CALLEE_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(CASCADE_VENDORS))
async def test_cascade_vendor_waits_for_the_callee(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    from .cascade_fakes import FakeLLM, tool, wait_until, with_fake_legs

    case = CASES[agent]
    llm = FakeLLM(["Hi, this is the agent.", tool("end_call")])
    provider = CASCADE_VENDORS[name]()
    stt, _ = with_fake_legs(provider, llm)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    await line.incoming.put(HELLO)  # the callee answers: straight to STT, nothing asks the model
    await wait_until(lambda: stt.fed)
    await asyncio.sleep(0.05)
    assert llm.requests == [] and line.sent == []

    await stt.turns.put("Hello?")
    await asyncio.wait_for(task, 3)
    ((messages, tools),) = llm.requests
    assert messages == [
        {"role": "system", "content": case.setup().instructions},
        {"role": "user", "content": "Hello?"},
    ]
    assert _tools_of(tools) == case.expected_tools()
    assert line.sent and line.closed
    assert [c for c, _ in session.calls] == ["end_call"]


@pytest.mark.parametrize("name", sorted(CASCADE_VENDORS))
async def test_cascade_vendor_starts_with_remis_screen_pop(name: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    from .cascade_fakes import FakeLLM, tool, wait_until, with_fake_legs

    events: list[str] = []

    class RecordingLLM(FakeLLM):
        async def reply(self, messages, tools):
            events.append("llm")
            async for part in super().reply(messages, tools):
                yield part

    llm = RecordingLLM(["Hi.", tool("take_payment")])
    provider = CASCADE_VENDORS[name]()
    stt, _ = with_fake_legs(provider, llm)
    line = FakeLine(events)
    call = await remi_screen_pop_runtime(events, line)
    task = asyncio.create_task(provider.run_call(call))
    await stt.turns.put("Hello?")
    await wait_until(lambda: call.session.results)  # type: ignore[attr-defined]

    system = llm.requests[0][0][0]
    assert system == {"role": "system", "content": call.setup.instructions}
    assert (
        "# Account on screen" in system["content"] and "You are calling Marcus Sokolov" in system["content"]
    )
    for secret in REMI_SECRETS:
        assert secret not in system["content"]
    reads = [i for i, e in enumerate(events) if e.startswith("sql:")]
    assert {events[i] for i in reads} >= {"sql:accounts", "sql:phones", "sql:contact_attempts"}
    assert max(reads) < events.index("llm") < events.index("agent audio")
    ((tool_name, refusal),) = call.session.results  # type: ignore[attr-defined]
    assert tool_name == "take_payment" and refusal["error"].startswith("Not verified yet")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
@pytest.mark.parametrize("name", sorted(CASCADE_VENDORS))
async def test_cascade_vendor_lets_an_inbound_agent_speak_first(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    from .cascade_fakes import FakeLLM, tool, wait_until, with_fake_legs

    case = CASES[agent]
    llm = FakeLLM(["Thanks for calling."], [tool("verify_caller", "{}")], ["Bye.", tool("end_call")])
    provider = CASCADE_VENDORS[name]()
    stt, _ = with_fake_legs(provider, llm)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(provider.run_call(call))

    await wait_until(lambda: line.sent)  # the greeting, before the caller has said anything
    opening = llm.requests[0][0]
    assert opening == [
        {"role": "system", "content": case.setup().instructions},
        {"role": "system", "content": case.policy.opening_instruction},
    ]
    assert _tools_of(llm.requests[0][1]) == case.expected_tools()
    await stt.turns.put("My account is ACME-1.")
    await asyncio.wait_for(task, 3)
    assert line.closed
    assert [c for c, _ in session.calls] == ["verify_caller", "end_call"]


# --- gradium: the session gradbot is started with -----------------------------------------


def _gradbot_tools(config: Any) -> dict[str, tuple[set[str], set[str]]]:
    import json

    out = {}
    for t in config.tools:
        params = json.loads(t.parameters_json)
        out[t.name] = (set(params["properties"]), set(params["required"]))
    return out


@pytest.mark.parametrize("agent", CALLEE_FIRST_CASES)
async def test_gradium_waits_for_the_callee(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if not installed("gradium"):
        pytest.skip("gradium extra not installed")
    from vamos_voice.providers.gradium import GradiumConfig, GradiumProvider

    from .cascade_fakes import wait_until
    from .gradbot_fakes import AUDIO_48K, FakeGradbot

    case = CASES[agent]
    gradbot = FakeGradbot().install(monkeypatch)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(case, line, session)
    task = asyncio.create_task(GradiumProvider(GradiumConfig("grad-test", "sk-test")).run_call(call))
    await gradbot.started.wait()
    config = gradbot.config
    assert config.instructions == case.setup().instructions
    assert _gradbot_tools(config) == case.expected_tools()
    assert config.assistant_speaks_first is False and config.silence_timeout_s == 0.0
    assert gradbot.kwargs["llm_model_name"] == "gpt-4.1-mini"

    await line.incoming.put(HELLO)
    await wait_until(lambda: gradbot.caller_audio)
    assert gradbot.caller_audio == [HELLO], "caller audio reaches gradbot untouched"
    gradbot.audio()  # anything before the callee is heard is dropped
    await asyncio.sleep(0.05)
    assert line.sent == []
    gradbot.stt("Hello?")
    gradbot.audio()
    await wait_until(lambda: line.sent)
    assert len(line.sent[0]) == len(AUDIO_48K) // 2, "48 kHz out becomes 24 kHz"

    gradbot.tool_call("end_call")
    gradbot.event("end_tts_audio")
    await asyncio.wait_for(task, 3)
    assert line.closed and gradbot.closed
    assert [c for c, _ in session.calls] == ["end_call"]


async def test_gradium_starts_with_remis_screen_pop(monkeypatch: pytest.MonkeyPatch) -> None:
    if not installed("gradium"):
        pytest.skip("gradium extra not installed")
    from vamos_voice.providers.gradium import GradiumConfig, GradiumProvider

    from .gradbot_fakes import FakeGradbot

    events: list[str] = []
    gradbot = FakeGradbot().install(monkeypatch)
    call = await remi_screen_pop_runtime(events, FakeLine(events))
    task = asyncio.create_task(GradiumProvider(GradiumConfig("grad-test", "sk-test")).run_call(call))
    await gradbot.started.wait()
    instructions = gradbot.config.instructions
    assert instructions == call.setup.instructions and "# Account on screen" in instructions
    assert not any(secret in instructions for secret in REMI_SECRETS)
    assert {"sql:accounts", "sql:phones", "sql:contact_attempts"} <= set(events)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("agent", AGENT_FIRST_CASES)
async def test_gradium_lets_an_inbound_agent_speak_first(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if not installed("gradium"):
        pytest.skip("gradium extra not installed")
    from vamos_voice.providers.gradium import GradiumConfig, GradiumProvider

    from .cascade_fakes import wait_until
    from .gradbot_fakes import FakeGradbot

    case = CASES[agent]
    gradbot = FakeGradbot().install(monkeypatch)
    line = FakeLine()
    call = runtime(case, line, ScriptedSession())
    task = asyncio.create_task(GradiumProvider(GradiumConfig("grad-test", "sk-test")).run_call(call))
    await gradbot.started.wait()
    config = gradbot.config
    assert config.assistant_speaks_first is True
    assert config.instructions == f"{case.setup().instructions}\n\n{case.policy.opening_instruction}"
    gradbot.audio()  # the greeting reaches the caller before they speak
    await wait_until(lambda: line.sent)
    gradbot.end()
    await asyncio.wait_for(task, 3)


# --- hosted orchestrators and LiveKit: what each vendor is handed ----------------------------
# (their scripted calls, webhook and resource lifecycles are in tests/test_hosted_vendors.py)


def _vapi_handed(case: AgentCase) -> dict[str, Any]:
    from vamos_voice.providers.vapi import VapiProvider

    assistant = VapiProvider("vapi-test", "https://pod.example")
    model = assistant.assistant(case.setup(), "s3cret")["model"]
    tools = model["tools"]
    assert {t["server"]["url"] for t in tools} == {"https://pod.example/vendor/vapi/tool"}
    return {
        "instructions": model["messages"][0]["content"],
        "tools": {
            t["function"]["name"]: (
                set(t["function"]["parameters"]["properties"]),
                set(t["function"]["parameters"]["required"]),
            )
            for t in tools
        },
    }


def _cartesia_handed(case: AgentCase) -> dict[str, Any]:
    from vamos_voice.providers.cartesia import CartesiaProvider, tool_name

    provider = CartesiaProvider("ct-test")
    setup = case.setup()
    definitions = provider.tool_format.convert(setup.tools)
    return {
        "instructions": provider.agent_config(setup, [])["instructions"],
        "tools": {
            tool_name(d["name"]): (set(d["parameters"]["properties"]), set(d["parameters"]["required"]))
            for d in definitions
        },
    }


def _livekit_handed(case: AgentCase) -> dict[str, Any]:
    import asyncio as _asyncio

    from livekit.agents.llm.tool_context import get_raw_function_info

    from vamos_voice.providers.livekit import LiveKitConfig, LiveKitProvider

    provider = LiveKitProvider(LiveKitConfig("sk-test", "dg-test", "el-test"))
    agent = provider.build_agent(runtime(case), _asyncio.Event())
    raw = [get_raw_function_info(t).raw_schema for t in agent.tools]
    return {
        "instructions": agent.instructions,
        "tools": {
            r["name"]: (set(r["parameters"]["properties"]), set(r["parameters"]["required"])) for r in raw
        },
    }


HANDED = {"vapi": _vapi_handed, "cartesia": _cartesia_handed, "livekit": _livekit_handed}


@pytest.mark.parametrize("agent", sorted(CASES))
@pytest.mark.parametrize("name", sorted(HANDED))
def test_hosted_and_livekit_vendors_get_the_same_instructions_and_tools(name: str, agent: str) -> None:
    if not installed(name):
        pytest.skip(f"{name} extra not installed")
    case = CASES[agent]
    if case.policy.agent_opens and not load_provider(name).opens_calls:
        pytest.skip(f"{name} cannot open a call (refused at startup)")
    handed = HANDED[name](case)
    assert handed["instructions"] == case.setup().instructions
    assert handed["tools"] == case.expected_tools()
