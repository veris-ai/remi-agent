"""Vapi, Cartesia and LiveKit, offline: what each sends, its tool path, its hang-up and its vendor resources.

Vapi and Cartesia run whole scripted calls against a fake vendor socket and an
``httpx.MockTransport`` standing in for the vendor's REST API; Vapi's tool
webhook is called through the real server app. LiveKit (WebRTC, a local room
server) is checked on what it hands LiveKit Agents and on its tool handlers.
No test reaches a network.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from vamos_voice.framing import Framing
from vamos_voice.providers import load_provider

from .fakes import FAKE_INBOUND, HELLO, REMI, FakeLine, ScriptedSession, remi_gated_session, runtime

SPEECH = b"\x10\x00" * 2400  # 100 ms of audible PCM16
SILENCE = b"\x00\x00" * 2400


def installed(name: str) -> bool:
    try:
        load_provider(name)
    except ImportError:
        return False
    return True


needs_vapi = pytest.mark.skipif(not installed("vapi"), reason="vapi extra not installed")
needs_cartesia = pytest.mark.skipif(not installed("cartesia"), reason="cartesia extra not installed")
needs_livekit = pytest.mark.skipif(not installed("livekit"), reason="livekit extra not installed")


class MixedSocket:
    """A vendor socket carrying binary audio and JSON text both ways; records what was sent."""

    def __init__(self) -> None:
        self.sent: list[bytes | dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.url = ""
        self.headers: dict[str, str] = {}

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message if isinstance(message, bytes) else json.loads(message))

    async def recv(self) -> str | bytes:
        message = await self.inbox.get()
        if message is None:
            import websockets

            raise websockets.ConnectionClosedOK(None, None)
        return message

    def push(self, event: dict[str, Any] | bytes) -> None:
        self.inbox.put_nowait(event if isinstance(event, bytes) else json.dumps(event))

    def frames(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if isinstance(m, dict)]

    def connector(self) -> Callable[..., Any]:
        @asynccontextmanager
        async def connect(url: str, headers: dict[str, str]):
            self.url, self.headers = url, dict(headers)
            yield self

        return connect


async def until(predicate: Callable[[], bool], within: float = 3.0) -> None:
    """Poll a condition the code under test reaches by itself (no event to wait on)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.01)


class RestFake:
    """A vendor REST API: records every request, answers with ``handler``."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


# --- Vapi ---------------------------------------------------------------------------------


def _vapi(sock: MixedSocket) -> tuple[Any, RestFake]:
    from vamos_voice.providers.vapi import VapiProvider

    rest = RestFake(
        lambda r: httpx.Response(
            201,
            json={"id": "call_1", "transport": {"websocketCallUrl": "wss://api.vapi.ai/call_1/transport"}},
        )
    )
    provider = VapiProvider("vapi-key", "https://pod.example/hooks/att_x")
    provider.http = lambda: rest.client(base_url="https://api.vapi.ai")
    provider.connect = sock.connector()
    return provider, rest


@needs_vapi
def test_vapi_needs_its_key_and_a_public_https_url() -> None:
    from vamos_voice.providers.vapi import VapiProvider

    assert VapiProvider.required_env == ("VAPI_API_KEY", "PUBLIC_BASE_URL")
    assert VapiProvider.trace_adapter == "vapi"
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        VapiProvider.from_env({"VAPI_API_KEY": "k", "PUBLIC_BASE_URL": "http://pod.example"})
    provider = VapiProvider.from_env(
        {"VAPI_API_KEY": "k", "PUBLIC_BASE_URL": "https://pod.example/hooks/att_x/"}
    )
    assert provider.tool_url == "https://pod.example/hooks/att_x/vendor/vapi/tool"
    assert provider.config.model == "gpt-4.1-mini"


@needs_vapi
def test_vapi_assistant_waits_for_the_callee_and_carries_the_secret_only_in_tool_servers() -> None:
    from vamos_voice.providers.vapi import CLIENT_MESSAGES, SECRET_HEADER, VapiProvider

    provider = VapiProvider("vapi-key", "https://pod.example")
    request = provider.call_request(REMI.setup(), "s3cret")
    assert request["transport"]["audioFormat"] == {
        "format": "pcm_s16le",
        "container": "raw",
        "sampleRate": 24000,
    }
    assistant = request["assistant"]
    assert assistant["firstMessageMode"] == "assistant-waits-for-user"
    assert "firstMessage" not in assistant
    assert assistant["model"]["messages"] == [{"role": "system", "content": REMI.setup().instructions}]
    assert {t["server"]["headers"][SECRET_HEADER] for t in assistant["model"]["tools"]} == {"s3cret"}
    assert "s3cret" not in json.dumps({k: v for k, v in assistant.items() if k != "model"})
    # what the bench's ``vapi`` trace adapter reads off the socket
    assert {"transcript", "tool-calls", "tool-calls-result", "conversation-update", "status-update"} <= set(
        CLIENT_MESSAGES
    )
    assert assistant["startSpeakingPlan"]["waitSeconds"] == 0.8


@needs_vapi
def test_vapi_lets_an_inbound_agent_open_with_a_model_generated_message() -> None:
    from vamos_voice.providers.vapi import VapiProvider

    assistant = VapiProvider("vapi-key", "https://pod.example").assistant(FAKE_INBOUND.setup(), "s")
    assert assistant["firstMessageMode"] == "assistant-speaks-first-with-model-generated-message"
    assert assistant["model"]["messages"][1] == {
        "role": "system",
        "content": FAKE_INBOUND.policy.opening_instruction,
    }


@needs_vapi
async def test_vapi_call_holds_the_line_runs_webhook_tools_and_hangs_up() -> None:
    from vamos_voice.providers.vapi import SECRET_HEADER

    sock = MixedSocket()
    provider, rest = _vapi(sock)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider.run_call(call))
    await until(lambda: provider._live is not None and provider._live.vapi_call_id == "call_1")
    created = json.loads(rest.requests[0].content)
    assert rest.calls() == [("POST", "/call")]
    assert rest.requests[0].headers["Authorization"] == "Bearer vapi-key"
    secret = created["assistant"]["model"]["tools"][0]["server"]["headers"][SECRET_HEADER]
    assert sock.url == "wss://api.vapi.ai/call_1/transport" and sock.headers == {}

    await line.incoming.put(HELLO)
    await until(lambda: HELLO in sock.sent)  # caller audio goes straight to Vapi
    sock.push(SPEECH)  # anything Vapi plays before the callee is heard is held back
    await asyncio.sleep(0.05)
    assert line.sent == []
    sock.push({"type": "transcript", "role": "user", "transcriptType": "final", "transcript": "Hello?"})
    sock.push(SPEECH)
    await until(lambda: bool(line.sent))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(provider.router(), prefix="/vendor/vapi")
    body = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "call_1"},
            "toolCallList": [
                {"id": "t1", "type": "function", "function": {"name": "get_account", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "end_call", "arguments": {}}},
            ],
        }
    }

    def post(headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
        return TestClient(app).post("/vendor/vapi/tool", json=payload, headers=headers)

    loop = asyncio.get_running_loop()
    assert (await loop.run_in_executor(None, post, {}, body)).status_code == 401
    assert (await loop.run_in_executor(None, post, {SECRET_HEADER: "wrong"}, body)).status_code == 401
    other_call = {"message": {**body["message"], "call": {"id": "call_2"}}}
    assert (await loop.run_in_executor(None, post, {SECRET_HEADER: secret}, other_call)).status_code == 404
    assert session.calls == [], "a refused webhook runs nothing"

    response = await loop.run_in_executor(None, post, {SECRET_HEADER: secret}, body)
    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"toolCallId": "t1", "result": json.dumps({"ok": "get_account"})},
            {"toolCallId": "t2", "result": json.dumps({"ok": "end_call"})},
        ]
    }
    assert [name for name, _ in session.calls] == ["get_account", "end_call"]
    await asyncio.wait_for(task, 5)  # hang-up after the last words
    assert line.closed
    assert {"type": "end-call"} in sock.frames()
    assert provider._live is None
    assert (await loop.run_in_executor(None, post, {SECRET_HEADER: secret}, body)).status_code == 401


@needs_vapi
async def test_vapi_webhook_dispatches_through_the_call_session_with_its_gates_and_redaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from vamos_voice.providers.vapi import _LiveCall

    provider, _ = _vapi(MixedSocket())
    session = remi_gated_session()
    live = _LiveCall(runtime(REMI, FakeLine(), session), "s", "call_1")
    card = {"card_number": "4111111111111111", "expiry": "12/30", "cvc": "123", "zip": "11238"}
    results = await provider.run_tool_calls(
        live,
        [{"id": "t1", "function": {"name": "add_payment_method", "arguments": json.dumps(card)}}],
    )
    (result,) = results
    assert json.loads(result["result"])["error"]  # the gate refuses before verification
    assert "4111111111111111" not in caplog.text


@needs_vapi
async def test_vapi_caller_hang_up_ends_the_vapi_call() -> None:
    sock = MixedSocket()
    provider, _ = _vapi(sock)
    line = FakeLine()
    task = asyncio.create_task(provider.run_call(runtime(REMI, line, ScriptedSession())))
    await until(lambda: provider._live is not None and provider._live.vapi_call_id is not None)
    await line.incoming.put(None)  # the caller hangs up
    await asyncio.wait_for(task, 3)
    assert sock.frames()[-1] == {"type": "end-call"}


@needs_vapi
async def test_vapi_status_ended_ends_the_call() -> None:
    sock = MixedSocket()
    provider, _ = _vapi(sock)
    task = asyncio.create_task(provider.run_call(runtime(REMI, FakeLine(), ScriptedSession())))
    await until(lambda: provider._live is not None and provider._live.vapi_call_id is not None)
    sock.push({"message": {"type": "status-update", "status": "ended", "endedReason": "silence-timed-out"}})
    await asyncio.wait_for(task, 3)


@needs_vapi
async def test_vapi_refused_call_creation_fails_loudly() -> None:
    from vamos_voice.providers.vapi import VapiProvider

    provider = VapiProvider("bad", "https://pod.example")
    rest = RestFake(lambda r: httpx.Response(401, json={"message": "Invalid Key"}))
    provider.http = lambda: rest.client(base_url="https://api.vapi.ai")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await provider.run_call(runtime(REMI))
    assert provider._live is None


# --- Cartesia -----------------------------------------------------------------------------


class CartesiaRest:
    """Cartesia's tools and agents endpoints over an in-memory account."""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.tools = {t["id"]: t for t in existing or []}
        self.agents: dict[str, dict[str, Any]] = {}
        self.fake = RestFake(self.handle)
        self.n = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/agents/tools":
            return httpx.Response(200, json={"data": list(self.tools.values()), "has_more": False})
        if request.method == "POST" and path == "/v1/agents/tools":
            self.n += 1
            tool = {**json.loads(request.content), "id": f"agent_tool_{self.n}"}
            self.tools[tool["id"]] = tool
            return httpx.Response(201, json=tool)
        if request.method == "POST" and path == "/v1/agents":
            self.agents["agent_1"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "agent_1"})
        if request.method == "DELETE" and path == "/v1/agents/agent_1":
            self.agents.pop("agent_1")
            return httpx.Response(204)
        return httpx.Response(404)


def _cartesia(sock: MixedSocket, rest: CartesiaRest) -> Any:
    from vamos_voice.providers.cartesia import CartesiaProvider

    provider = CartesiaProvider("ct-key")
    provider.http = lambda: rest.fake.client(base_url="https://api.cartesia.ai")
    provider.connect = sock.connector()
    return provider


@needs_cartesia
def test_cartesia_tools_are_client_tools_cut_to_its_schema_and_renamed_off_its_system_tools() -> None:
    from vamos_voice.providers.cartesia import CartesiaProvider, tool_name, wire_name

    definitions = {d["name"]: d for d in CartesiaProvider("k").tool_format.convert(REMI.setup().tools)}
    assert "end_call" not in definitions and "end_call_" in definitions
    assert tool_name("end_call_") == "end_call" and tool_name("get_account") == "get_account"
    assert wire_name("transfer_to_human") == "transfer_to_human"
    allowed = {"type", "description", "enum", "items", "properties", "required"}

    def keys(node: Any) -> set[str]:
        if not isinstance(node, dict):
            return set()
        found = set(node) - {"properties"}
        for value in (node.get("properties") or {}).values():
            found |= keys(value)
        return found | keys(node.get("items"))

    for d in definitions.values():
        assert d["type"] == "client" and d["expects_response"] is True
        assert keys(d["parameters"]) <= allowed, d["name"]


@needs_cartesia
def test_cartesia_refuses_an_agent_that_speaks_first_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    import vamos_voice.server as server
    from vamos_voice.agent import ConfigError

    from .fake_inbound import FakeInboundAgent

    monkeypatch.setattr(server, "load_agent", lambda name: FakeInboundAgent)
    env = {"VAMOS_AGENT": "fake_inbound", "VAMOS_VENDOR": "cartesia", "CARTESIA_API_KEY": "k"}
    with pytest.raises(ConfigError, match="cartesia cannot open a call"):
        server.build_app_from_env(env)
    assert server.build_app_from_env({**env, "VAMOS_VENDOR": "openai_realtime", "OPENAI_API_KEY": "k"})


@needs_cartesia
async def test_cartesia_call_reuses_identical_tools_relays_audio_runs_client_tools_and_deletes_its_agent() -> (
    None
):
    from vamos_voice.providers.cartesia import CartesiaProvider

    tools = CartesiaProvider("k").tool_format.convert(REMI.setup().tools)
    existing = [{**tools[0], "id": "agent_tool_old", "created_at": "2026-10-01T00:00:00Z"}]
    rest = CartesiaRest(existing)
    sock = MixedSocket()
    provider = _cartesia(sock, rest)
    line, session = FakeLine(), ScriptedSession()
    call = runtime(REMI, line, session)
    task = asyncio.create_task(provider.run_call(call))

    await until(lambda: bool(sock.frames()))
    assert sock.frames()[0] == {
        "type": "session_create",
        "audio": {"input_format": "pcm_24000", "output_delivery": "speaking_pace"},
    }
    assert sock.url.startswith("wss://api.cartesia.ai/v1/agents/websocket/agent_1")
    assert sock.headers == {"X-API-Key": "ct-key"}
    created_tools = [r for r in rest.fake.requests if r.method == "POST" and r.url.path == "/v1/agents/tools"]
    assert len(created_tools) == len(tools) - 1, "the identical stored tool is reused, the rest created"
    config = rest.agents["agent_1"]["config"]
    assert config["initial_message"] is None
    assert config["instructions"] == REMI.setup().instructions
    assert config["tools"][0] == {"id": "agent_tool_old"} and len(config["tools"]) == len(tools)
    assert "system_tools" not in config

    sock.push({"type": "session_ready", "call_id": "ac_1"})
    await line.incoming.put(HELLO)
    await until(lambda: any(f.get("type") == "audio_input" for f in sock.frames()))
    sock.push({"type": "audio_output", "audio": base64.b64encode(SPEECH).decode()})
    await asyncio.sleep(0.05)
    assert line.sent == [], "agent audio before the callee spoke"
    sock.push({"type": "turn_ended", "role": "user", "turn": 1, "text": "Hello?"})
    sock.push({"type": "audio_output", "audio": base64.b64encode(SPEECH).decode()})
    await until(lambda: bool(line.sent))

    sock.push(
        {
            "type": "client_tool_call",
            "tool_call_id": "c1",
            "tool_name": "get_account",
            "parameters": {},
            "expects_response": True,
        }
    )
    await until(lambda: any(f.get("type") == "client_tool_result" for f in sock.frames()))
    assert sock.frames()[-1] == {
        "type": "client_tool_result",
        "tool_call_id": "c1",
        "result": json.dumps({"ok": "get_account"}),
        "is_error": False,
    }
    sock.push(
        {
            "type": "client_tool_call",
            "tool_call_id": "c2",
            "tool_name": "end_call_",
            "parameters": {},
            "expects_response": True,
        }
    )
    await asyncio.wait_for(task, 5)
    assert [name for name, _ in session.calls] == ["get_account", "end_call"]
    assert line.closed
    assert rest.agents == {}, "the call's agent is deleted"
    assert ("DELETE", "/v1/agents/agent_1") in rest.fake.calls()
    assert not any(m == "DELETE" and "/tools/" in p for m, p in rest.fake.calls()), "shared tools stay"

    # a second call on the same pod reuses every tool
    rest.fake.requests.clear()
    sock2 = MixedSocket()
    provider.connect = sock2.connector()
    task = asyncio.create_task(provider.run_call(runtime(REMI, FakeLine(), ScriptedSession())))
    await until(lambda: bool(sock2.frames()))
    sock2.inbox.put_nowait(None)
    with pytest.raises(Exception):  # noqa: B017 - the socket closed before session_ready
        await asyncio.wait_for(task, 3)
    assert ("POST", "/v1/agents/tools") not in rest.fake.calls()
    assert rest.agents == {}, "the agent is deleted even when the session never started"


@needs_cartesia
async def test_cartesia_tool_error_results_are_flagged() -> None:
    sock = MixedSocket()
    rest = CartesiaRest()
    provider = _cartesia(sock, rest)
    call = runtime(REMI, FakeLine(), remi_gated_session())
    task = asyncio.create_task(provider.run_call(call))
    await until(lambda: bool(sock.frames()))
    sock.push({"type": "session_ready", "call_id": "ac_1"})
    sock.push(
        {
            "type": "client_tool_call",
            "tool_call_id": "c1",
            "tool_name": "take_payment",
            "parameters": {"amount_cents": 100},
        }
    )
    await until(lambda: any(f.get("type") == "client_tool_result" for f in sock.frames()))
    result = next(f for f in sock.frames() if f.get("type") == "client_tool_result")
    assert result["is_error"] is True and "error" in json.loads(result["result"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert rest.agents == {}


# --- LiveKit ------------------------------------------------------------------------------


@needs_livekit
def test_livekit_maps_the_shared_turn_taking_and_runs_its_server_on_loopback_only() -> None:
    from vamos_voice.providers.base import TurnTaking
    from vamos_voice.providers.livekit import LiveKitConfig, LiveKitProvider, turn_handling, vad_options

    turns = TurnTaking()
    assert vad_options(turns) == {
        "min_silence_duration": 0.8,
        "prefix_padding_duration": 0.3,
        "activation_threshold": 0.5,
    }
    assert turn_handling(turns) == {"endpointing": {"min_delay": 0.8}}
    config = LiveKitConfig.from_env(
        {"OPENAI_API_KEY": "o", "DEEPGRAM_API_KEY": "d", "ELEVENLABS_API_KEY": "e"}
    )
    assert config.local_server and config.url == "ws://localhost:7880"
    assert len(config.room_api_secret) >= 32, "the local server's secret is minted per process"
    assert (config.llm_model, config.stt_model, config.tts_model) == (
        "gpt-4.1-mini",
        "nova-3-general",
        "eleven_flash_v2",
    )
    command = LiveKitProvider(config).server_command()
    assert command[command.index("--bind") + 1] == "127.0.0.1"
    assert f"{config.room_api_key}: {config.room_api_secret}" in command
    assert not LiveKitConfig.from_env(
        {
            "OPENAI_API_KEY": "o",
            "DEEPGRAM_API_KEY": "d",
            "ELEVENLABS_API_KEY": "e",
            "LIVEKIT_URL": "wss://lk.example",
        }
    ).local_server


@needs_livekit
def test_livekit_session_assembles_against_the_pinned_livekit_agents() -> None:
    from vamos_voice.providers.livekit import MAX_TOOL_STEPS, LiveKitConfig, LiveKitProvider

    provider = LiveKitProvider(LiveKitConfig("sk-test", "dg-test", "el-test"))

    async def build() -> Any:
        import aiohttp

        async with aiohttp.ClientSession() as http:
            return provider.build_session(REMI.setup(), http)

    session = asyncio.run(build())
    assert type(session).__name__ == "AgentSession"
    assert session.options.max_tool_steps == MAX_TOOL_STEPS


class _RunContext:
    def __init__(self) -> None:
        self.played = False

    async def wait_for_playout(self) -> None:
        self.played = True


@needs_livekit
async def test_livekit_tools_dispatch_through_the_session_and_the_hang_up_stops_the_reply() -> None:
    from livekit.agents.llm import StopResponse
    from livekit.agents.llm.tool_context import get_raw_function_info

    from vamos_voice.providers.livekit import LiveKitToolFormat

    session = ScriptedSession()
    ended = asyncio.Event()
    tools = {
        get_raw_function_info(t).name: t
        for t in LiveKitToolFormat(runtime(REMI, FakeLine(), session), ended).convert(REMI.setup().tools)
    }
    assert await tools["get_account"](raw_arguments={}, context=_RunContext()) == {"ok": "get_account"}
    assert not ended.is_set()
    context = _RunContext()
    with pytest.raises(StopResponse):
        await tools["end_call"](raw_arguments={}, context=context)
    assert context.played and ended.is_set()
    assert [name for name, _ in session.calls] == ["get_account", "end_call"]


# --- shared pieces ------------------------------------------------------------------------


async def test_quiet_wait_ignores_digital_silence() -> None:
    from vamos_voice.call_start import CallStart, callee_first
    from vamos_voice.providers.base import OutboundAudio

    clock = [0.0]
    start = CallStart(callee_first())
    start.mark_callee_spoke()
    out = OutboundAudio(FakeLine(), start, monotonic=lambda: clock[0])  # type: ignore[arg-type]
    await out.send(SPEECH)
    clock[0] = 0.5
    await out.send(SILENCE)  # a vendor streaming silence (Vapi) is quiet

    async def tick() -> None:
        while True:
            await asyncio.sleep(0.001)
            clock[0] += 0.05

    ticker = asyncio.create_task(tick())
    try:
        await asyncio.wait_for(out.wait_until_quiet(1.0, 15.0, poll_s=0.001), 2)
        assert 1.0 <= clock[0] < 2.0
    finally:
        ticker.cancel()


def test_server_mounts_provider_routes_and_closes_the_provider() -> None:
    from collections.abc import Mapping
    from typing import Self

    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from vamos_voice.providers.base import CallRuntime, VoiceProvider
    from vamos_voice.server import create_app

    class RoutedProvider(VoiceProvider):
        name = "routed"
        trace_adapter = None
        required_env = ()
        closed = False

        @classmethod
        def from_env(cls, env: Mapping[str, str]) -> Self:
            return cls()

        async def run_call(self, call: CallRuntime) -> None:
            return None

        async def close(self) -> None:
            RoutedProvider.closed = True

        def router(self) -> APIRouter:
            router = APIRouter()

            @router.post("/hook")
            async def hook() -> dict[str, str]:
                return {"ok": "hook"}

            return router

    with TestClient(create_app(FAKE_INBOUND.agent(), RoutedProvider(), framing=Framing.JSON)) as client:
        assert client.post("/vendor/routed/hook").json() == {"ok": "hook"}
        assert client.post("/hook").status_code == 404
    assert RoutedProvider.closed
