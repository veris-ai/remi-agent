"""The agent on ElevenLabs Conversational AI (``wss://api.elevenlabs.io/v1/convai/conversation``).

ElevenLabs runs ASR, the LLM (``gpt-4.1-mini``, as in the VAmoS Energy
ElevenLabs candidate), TTS and turn taking on its side, behind a stored
*agent* that holds the prompt and the tool declarations. Bench candidate:
``trace_adapter: elevenlabs-convai``, which reads
``conversation_initiation_client_data``, ``user_transcript``,
``agent_response``, ``client_tool_call`` and ``client_tool_result`` off the
``websockets`` client this provider uses (the raw protocol, not the SDK).

Per call (the bench runs one call per pod):

1. ``POST /v1/convai/agents/create``: an agent carrying this call's
   instructions (the screen pop is in them, so the agent exists only after
   the call connects), the tools as **client** tools (the platform sends each
   call down the conversation socket; no webhook, no inbound endpoint), no
   ``first_message`` and ``pcm_24000`` on both legs (no resampling). Its
   platform settings allow the prompt and first-message overrides.
2. The conversation socket, keyed by ``xi-api-key``. The first frame,
   ``conversation_initiation_client_data``, repeats the instructions as the
   prompt override (that is where the trace adapter reads the system prompt)
   and an empty ``first_message``.
3. ``DELETE`` the agent when the call ends, whatever happened.

Tools: ElevenLabs requires a ``description`` on every parameter; a parameter
the agent's schema leaves undescribed gets its own name (``ElevenLabsToolFormat``).
Tool calls arrive as ``client_tool_call`` and are answered with
``client_tool_result`` (``result`` is a string).

Turn taking is ElevenLabs' own learned turn model; it exposes no silence
threshold, so ``TurnTaking`` is not mapped (``turn_eagerness`` stays the
documented default, set explicitly). On ``interruption`` ElevenLabs stops its
reply; this provider only marks the playout as interrupted. ``ping`` is
answered with ``pong`` (the platform drops a socket that does not).

Call start: ``CALLEE_FIRST`` has no first message, so the agent says nothing
until the callee's first ``user_transcript`` (which also opens the outbound
gate). ``AGENT_FIRST``: the only way to ask the hosted LLM for a turn is a
``user_message``, so the opening goes in as the policy's labelled
``opening_note`` (as on Gemini Live).

Hang-up: once ``end_call`` has run, the line closes after the agent's audio
has gone quiet (``HANGUP_QUIET_S`` with no audio, at most ``HANGUP_WAIT_S``)
and the last words have played: the protocol has no end-of-audio event.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol, Self

import httpx
import websockets

from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
URL = "wss://api.elevenlabs.io/v1/convai/conversation"
HTTP_TIMEOUT_S = 30.0
AUDIO_FORMAT = "pcm_24000"
#: The LLM of the VAmoS Energy ElevenLabs candidate.
DEFAULT_LLM = "gpt-4.1-mini"
#: English ConvAI agents reject the v2_5 / v3 TTS variants.
DEFAULT_TTS_MODEL = "eleven_flash_v2"
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # "Sarah", as on VAmoS Energy
HANGUP_QUIET_S = 0.6
HANGUP_WAIT_S = 10.0


class ElevenLabsToolFormat:
    """Client tools for the stored agent; every parameter carries a ``description`` (ElevenLabs
    rejects one without), the agent's own wherever it wrote one."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "client",
                "name": t.name,
                "description": t.description,
                "parameters": _described(t.parameters),
                "expects_response": True,  # hold the turn until the result is back
            }
            for t in tools
        ]


def _described(node: Any, name: str = "") -> Any:
    if not isinstance(node, dict):
        return node
    out = {
        key: {prop: _described(sub, prop) for prop, sub in value.items()}
        if key == "properties"
        else _described(value)
        for key, value in node.items()
    }
    if name and not out.get("description"):
        out["description"] = name
    return out


class AgentStore(Protocol):
    """Where the per-call agent is created and deleted (tests replace it)."""

    async def create(self, body: dict[str, Any]) -> str: ...
    async def delete(self, agent_id: str) -> None: ...


class HttpAgentStore:
    """The ConvAI agents REST API."""

    def __init__(self, api_key: str) -> None:
        self._headers = {"xi-api-key": api_key}

    async def create(self, body: dict[str, Any]) -> str:
        async with httpx.AsyncClient(base_url=API, headers=self._headers, timeout=HTTP_TIMEOUT_S) as http:
            response = await http.post("/v1/convai/agents/create", json=body)
            if response.is_error:
                raise RuntimeError(f"elevenlabs agent create failed: {response.status_code} {response.text}")
            return response.json()["agent_id"]

    async def delete(self, agent_id: str) -> None:
        async with httpx.AsyncClient(base_url=API, headers=self._headers, timeout=HTTP_TIMEOUT_S) as http:
            response = await http.delete(f"/v1/convai/agents/{agent_id}")
            if response.is_error and response.status_code != 404:
                raise RuntimeError(f"elevenlabs agent delete failed: {response.status_code}")


class ElevenLabsProvider(VoiceProvider):
    name = "elevenlabs"
    trace_adapter = "elevenlabs-convai"
    required_env = ("ELEVENLABS_API_KEY",)
    tool_format = ElevenLabsToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, llm: str, tts_model: str, voice_id: str) -> None:
        self._api_key = api_key
        self.llm, self.tts_model, self.voice_id = llm, tts_model, voice_id
        self.agents: AgentStore = HttpAgentStore(api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["ELEVENLABS_API_KEY"],
            llm=env.get("VAMOS_ELEVENLABS_LLM") or DEFAULT_LLM,
            tts_model=env.get("VAMOS_ELEVENLABS_TTS_MODEL") or DEFAULT_TTS_MODEL,
            voice_id=env.get("VAMOS_ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID,
        )

    def agent_body(self, setup: SessionSetup) -> dict[str, Any]:
        """The per-call agent: this call's instructions and tools, no first message."""
        return {
            "name": "vamos-agents call",
            "tags": ["vamos-agents"],
            "conversation_config": {
                "agent": {
                    "first_message": "",
                    "language": "en",
                    "prompt": {
                        "prompt": setup.instructions,
                        "llm": self.llm,
                        "tools": self.tool_format.convert(setup.tools),
                        # Otherwise the platform prepends its own generic persona.
                        "ignore_default_personality": True,
                    },
                },
                "tts": {
                    "model_id": self.tts_model,
                    "voice_id": self.voice_id,
                    "agent_output_audio_format": AUDIO_FORMAT,
                },
                "asr": {"quality": "high", "user_input_audio_format": AUDIO_FORMAT},
                "turn": {"turn_eagerness": "normal"},
            },
            "platform_settings": {
                "overrides": {
                    "conversation_config_override": {
                        "agent": {"prompt": {"prompt": True}, "first_message": True}
                    }
                }
            },
        }

    @staticmethod
    def initiation(setup: SessionSetup) -> dict[str, Any]:
        """The socket's first frame: the instructions again, where the trace adapter reads them."""
        return {
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {
                "agent": {"prompt": {"prompt": setup.instructions}, "first_message": ""}
            },
        }

    async def run_call(self, call: CallRuntime) -> None:
        agent_id = await self.agents.create(self.agent_body(call.setup))
        logger.info("elevenlabs: created agent %s for this call", agent_id)
        try:
            async with self.connect(f"{URL}?agent_id={agent_id}", {"xi-api-key": self._api_key}) as ws:
                await _ConvaiCall(self, ws, call).run()
        finally:
            try:
                await self.agents.delete(agent_id)
            except (httpx.HTTPError, RuntimeError):
                logger.exception("elevenlabs: could not delete agent %s", agent_id)


class _ConvaiCall:
    """One call's conversation socket: the event loop and its per-call state."""

    def __init__(self, provider: ElevenLabsProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.turn = 0
        self.audio_at = 0.0
        # Calls run in order: the platform may send several at once, and they share the call's state.
        self.tool_lock = asyncio.Lock()

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> dict[str, Any] | None:
        try:
            raw = await self.ws.recv()
        except websockets.ConnectionClosed as closed:
            logger.info("elevenlabs socket closed: %s", closed)
            return None
        return json.loads(raw)

    async def run(self) -> None:
        call = self.call
        await self.send(self.provider.initiation(call.setup))
        while (event := await self.recv()) is not None and event.get(
            "type"
        ) != "conversation_initiation_metadata":
            if event.get("type") == "error":
                raise RuntimeError(f"elevenlabs error before the conversation started: {event}")
        if event is None:
            raise RuntimeError("ElevenLabs closed the conversation before it started")
        meta = event.get("conversation_initiation_metadata_event") or {}
        formats = (meta.get("user_input_audio_format"), meta.get("agent_output_audio_format"))
        if formats != (AUDIO_FORMAT, AUDIO_FORMAT):
            raise RuntimeError(f"elevenlabs audio formats {formats}, expected {AUDIO_FORMAT} both ways")
        logger.info("elevenlabs: conversation %s", meta.get("conversation_id"))

        async def forward(pcm: bytes) -> None:
            await self.send({"user_audio_chunk": base64.b64encode(pcm).decode()})

        call.inbound.bind(forward)
        if call.setup.start_policy.agent_opens:
            await self.send({"type": "user_message", "text": call.setup.start_policy.opening_note})
        await run_until_first_done(caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events())

    async def events(self) -> None:
        while (event := await self.recv()) is not None:
            if await self.on_event(event):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "audio":
            audio = (event.get("audio_event") or {}).get("audio_base_64")
            if audio:
                self.audio_at = asyncio.get_running_loop().time()
                await call.outbound.send(base64.b64decode(audio), f"turn{self.turn}")
        elif kind == "user_transcript":
            text = (event.get("user_transcription_event") or {}).get("user_transcript") or ""
            if text.strip():
                call.start.mark_callee_spoke()
        elif kind == "agent_response":
            self.turn += 1
        elif kind == "interruption":
            call.outbound.interrupted()
        elif kind == "ping":
            ping = event.get("ping_event") or {}
            await self.send({"type": "pong", "event_id": ping.get("event_id")})
        elif kind == "client_tool_call":
            await self.run_tool(event.get("client_tool_call") or {})
            if call.session.ended:
                await self.hang_up()
                return True
        elif kind == "error":
            raise RuntimeError(f"elevenlabs error: {event}")
        return False

    async def run_tool(self, tool_call: dict[str, Any]) -> None:
        async with self.tool_lock:
            result = await self.call.session.dispatch(
                tool_call["tool_name"], tool_call.get("parameters") or {}
            )
        await self.send(
            {
                "type": "client_tool_result",
                "tool_call_id": tool_call["tool_call_id"],
                "result": json.dumps(result, default=str),
                "is_error": "error" in result,
            }
        )

    async def hang_up(self) -> None:
        """Let the agent's last audio arrive (it streams faster than it plays), then hang up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANGUP_WAIT_S
        self.audio_at = max(self.audio_at, loop.time())  # the quiet window starts at the tool result

        async def audio_quiet() -> None:
            while loop.time() < deadline and loop.time() - self.audio_at < HANGUP_QUIET_S:
                event = await self.recv_within(HANGUP_QUIET_S)
                if event is None:
                    return
                if event.get("type") == "audio":
                    await self.on_event(event)

        await audio_quiet()
        await self.call.hang_up()

    async def recv_within(self, seconds: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.recv(), seconds)
        except TimeoutError:
            return {}


PROVIDER = ElevenLabsProvider
