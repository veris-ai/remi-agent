"""The agent on Vapi, a hosted orchestrator: Deepgram STT, an OpenAI LLM and ElevenLabs TTS run on Vapi.

Per call, ``POST /call`` creates a Vapi call with an **inline** assistant (the
session instructions with the screen pop, the agent's tools, the voice
stack) on Vapi's WebSocket transport at the channel's 24 kHz PCM16, so audio
passes through untouched both ways. Nothing is stored at Vapi but the call
record: an inline assistant is not a resource that outlives the call.

Tools. Vapi returns a tool result to the model only for a *server* tool: it
POSTs a ``tool-calls`` message to the tool's ``server.url`` and adds each
``result`` string to the conversation. So the pod needs a public address
(``PUBLIC_BASE_URL``, set by Veris Bench for a candidate with
``"public_endpoint": true``) and serves ``POST /vendor/vapi/tool``
(``VoiceProvider.router``). The route is authenticated with a secret minted
per call and sent to Vapi only inside that call's assistant
(``server.headers``); it also checks the message names the live call, then
runs every tool through the call's own ``CallSession.dispatch``, one at a
time. Anything else gets 401/404 and runs nothing.

Call start: ``firstMessageMode = assistant-waits-for-user`` for a callee-first
agent (Vapi's native "wait for the user"); for an agent that speaks first,
``assistant-speaks-first-with-model-generated-message`` with the opening
instruction as a second system message. Turn taking, interruptions and noise
handling are Vapi's own: no ``startSpeakingPlan``, so Vapi's defaults decide
when the caller's turn has ended.

Trace: ``trace_adapter = "vapi"`` reads the transport socket's JSON messages
(``transcript``, ``speech-update``, ``tool-calls``, ``tool-calls-result``,
``conversation-update``, ``status-update``), so the assistant subscribes them
in ``clientMessages`` and the socket is opened with ``wire.connect_websocket``.

Hang-up: when the agent's hang-up tool has run, the provider waits for Vapi's
audio to stop, lets it play, closes the caller's line and sends Vapi
``end-call``. A caller hang-up ends the Vapi call the same way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Self

import httpx
import websockets
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider, vendor_route_path
from vamos_voice.providers.hosted import hang_up_after_last_words, public_base_url, secret_matches
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

API_BASE = "https://api.vapi.ai"
API_TIMEOUT_S = 30.0
TOOL_ROUTE = "/tool"
#: The header carrying the per-call secret on every tool webhook.
SECRET_HEADER = "X-Vamos-Tool-Secret"
#: How long Vapi waits for a tool webhook (its default, 20 s, is tight for a payment).
TOOL_TIMEOUT_S = 30
SAMPLE_RATE_HZ = 24_000
#: What the transport socket must carry for the ``vapi`` trace adapter (and for this provider).
CLIENT_MESSAGES = [
    "transcript",
    "speech-update",
    "status-update",
    "tool-calls",
    "tool-calls-result",
    "conversation-update",
    "user-interrupted",
]


@dataclass(frozen=True)
class VapiConfig:
    """The voice stack Vapi runs (defaults: the VAmoS Energy Vapi candidate)."""

    model_provider: str = "openai"
    model: str = "gpt-4.1-mini"
    #: Vapi requires a per-turn cap (default 250, which cuts longer turns and tool arguments short); this is
    #: its maximum, the nearest to the other vendors, which set none.
    max_tokens: int = 10_000
    transcriber_provider: str = "deepgram"
    transcriber_model: str = "nova-3"
    voice_provider: str = "11labs"
    voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    voice_model: str = "eleven_flash_v2"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> VapiConfig:
        d = cls()
        return cls(
            model_provider=env.get("VAMOS_VAPI_MODEL_PROVIDER") or d.model_provider,
            model=env.get("VAMOS_VAPI_MODEL") or d.model,
            transcriber_provider=env.get("VAMOS_VAPI_TRANSCRIBER_PROVIDER") or d.transcriber_provider,
            transcriber_model=env.get("VAMOS_VAPI_TRANSCRIBER_MODEL") or d.transcriber_model,
            voice_provider=env.get("VAMOS_VAPI_VOICE_PROVIDER") or d.voice_provider,
            voice_id=env.get("VAMOS_VAPI_VOICE_ID") or d.voice_id,
            voice_model=env.get("VAMOS_VAPI_VOICE_MODEL") or d.voice_model,
        )


class VapiToolFormat:
    """OpenAI-style ``function`` tools, each a server tool posting to this pod's webhook."""

    def __init__(self, server: dict[str, Any]) -> None:
        self.server = server

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                "server": self.server,
            }
            for t in tools
        ]


@dataclass
class _LiveCall:
    """The one call this pod is on, as the webhook sees it."""

    runtime: CallRuntime
    secret: str
    vapi_call_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ended: asyncio.Event = field(default_factory=asyncio.Event)


class VapiProvider(VoiceProvider):
    name = "vapi"
    trace_adapter = "vapi"
    required_env = ("VAPI_API_KEY", "PUBLIC_BASE_URL")
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, public_url: str, config: VapiConfig | None = None) -> None:
        self._api_key = api_key
        self.public_url = public_url
        self.config = config or VapiConfig()
        self._live: _LiveCall | None = None
        #: The Vapi REST client for one call; tests swap in an ``httpx.MockTransport``.
        self.http: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
            base_url=API_BASE, timeout=API_TIMEOUT_S
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(env["VAPI_API_KEY"], public_base_url(env), VapiConfig.from_env(env))

    @property
    def tool_url(self) -> str:
        return self.public_url + vendor_route_path(self.name, TOOL_ROUTE)

    # --- what Vapi is sent -----------------------------------------------------------------

    def assistant(self, setup: SessionSetup, secret: str) -> dict[str, Any]:
        """The inline assistant for one call (the parity tests read it back)."""
        c = self.config
        server = {"url": self.tool_url, "timeoutSeconds": TOOL_TIMEOUT_S, "headers": {SECRET_HEADER: secret}}
        messages = [{"role": "system", "content": setup.instructions}]
        policy = setup.start_policy
        if policy.agent_opens:
            messages.append({"role": "system", "content": policy.opening_instruction})
        return {
            "firstMessageMode": (
                "assistant-speaks-first-with-model-generated-message"
                if policy.agent_opens
                else "assistant-waits-for-user"
            ),
            "model": {
                "provider": c.model_provider,
                "model": c.model,
                "maxTokens": c.max_tokens,
                "messages": messages,
                "tools": VapiToolFormat(server).convert(setup.tools),
            },
            "voice": {"provider": c.voice_provider, "voiceId": c.voice_id, "model": c.voice_model},
            "transcriber": {
                "provider": c.transcriber_provider,
                "model": c.transcriber_model,
                "language": "en",
            },
            "maxDurationSeconds": 1800,
            "clientMessages": CLIENT_MESSAGES,
        }

    def call_request(self, setup: SessionSetup, secret: str) -> dict[str, Any]:
        """``POST /call``: the inline assistant on Vapi's WebSocket transport at the channel's rate."""
        return {
            "transport": {
                "provider": "vapi.websocket",
                "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": SAMPLE_RATE_HZ},
            },
            "assistant": self.assistant(setup, secret),
        }

    # --- the tool webhook ------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(TOOL_ROUTE)
        async def tool_calls(request: Request) -> JSONResponse:
            live = self._live
            if live is None or not secret_matches(request.headers.get(SECRET_HEADER), live.secret):
                logger.warning("vapi tool webhook refused: no live call or wrong secret")
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                message = (await request.json())["message"]
                call_id = message["call"]["id"]
                tool_calls = message["toolCallList"]
            except (ValueError, KeyError, TypeError):
                return JSONResponse({"error": "not a tool-calls message"}, status_code=400)
            if call_id != live.vapi_call_id:
                logger.warning("vapi tool webhook for call %s, not the live call", call_id)
                return JSONResponse({"error": "unknown call"}, status_code=404)
            return JSONResponse({"results": await self.run_tool_calls(live, tool_calls)})

        return router

    async def run_tool_calls(self, live: _LiveCall, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One ``tool-calls`` message, in order, through the call's ``CallSession``. Vapi accepts
        only string results (anything else is "No result returned")."""
        results = []
        async with live.lock:
            for tc in tool_calls:
                function = tc.get("function") or {}
                raw = function.get("arguments")
                try:
                    args = json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})
                except json.JSONDecodeError:
                    args = None
                if args is None:
                    result: dict[str, Any] = {"error": "The tool arguments were not valid JSON."}
                else:
                    # Shielded: a payment that has started must finish even if Vapi gives up waiting.
                    result = await asyncio.shield(
                        live.runtime.session.dispatch(function.get("name", ""), args)
                    )
                results.append({"toolCallId": tc.get("id"), "result": json.dumps(result, default=str)})
            if live.runtime.session.ended:
                live.ended.set()
        return results

    # --- the call --------------------------------------------------------------------------

    async def run_call(self, call: CallRuntime) -> None:
        live = _LiveCall(call, secrets.token_urlsafe(32))
        self._live = live
        try:
            async with self.http() as api:
                response = await api.post(
                    "/call",
                    json=self.call_request(call.setup, live.secret),
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            if response.is_error:
                raise RuntimeError(
                    f"Vapi refused the call: HTTP {response.status_code} {response.text[:500]}"
                )
            created = response.json()
            live.vapi_call_id = created["id"]
            url = created["transport"]["websocketCallUrl"]
            logger.info("vapi call %s created", live.vapi_call_id)
            async with self.connect(url, {}) as ws:
                await _VapiCall(ws, live).run()
        finally:
            self._live = None


class _VapiCall:
    def __init__(self, ws: Any, live: _LiveCall) -> None:
        self.ws, self.live, self.call = ws, live, live.runtime
        self.turn = 0

    async def run(self) -> None:
        self.call.inbound.bind(self.ws.send)  # binary PCM16 at 24 kHz, as created
        try:
            await run_until_first_done(
                caller_to_vendor=self.call.inbound.run(),
                vendor_to_caller=self.events(),
                agent_hang_up=self.hang_up_when_ended(),
            )
        finally:
            await self.end_vapi_call()

    async def hang_up_when_ended(self) -> None:
        await self.live.ended.wait()
        await hang_up_after_last_words(self.call)

    async def end_vapi_call(self) -> None:
        """Tell Vapi the call is over (it may already have closed the socket: then it is)."""
        try:
            await self.ws.send(json.dumps({"type": "end-call"}))
        except websockets.ConnectionClosed:
            logger.info("vapi socket already closed at end of call")

    async def events(self) -> None:
        try:
            while True:
                raw = await self.ws.recv()
                if isinstance(raw, bytes | bytearray):
                    await self.call.outbound.send(bytes(raw), f"turn{self.turn}")
                elif self.on_message(json.loads(raw)):
                    return
        except websockets.ConnectionClosed as closed:
            logger.info("vapi socket closed: %s", closed)

    def on_message(self, event: dict[str, Any]) -> bool:
        """One control message; True when Vapi has ended the call."""
        message = event.get("message", event)
        kind = message.get("type")
        role = message.get("role")
        if kind == "transcript" and role == "user" and (message.get("transcript") or "").strip():
            self.call.start.mark_callee_spoke()
        elif kind == "speech-update" and role == "assistant" and message.get("status") == "started":
            self.turn += 1
        elif kind == "user-interrupted":
            self.call.outbound.interrupted()
        elif kind == "status-update" and message.get("status") == "ended":
            logger.info("vapi ended the call: %s", message.get("endedReason"))
            return True
        return False


PROVIDER = VapiProvider
