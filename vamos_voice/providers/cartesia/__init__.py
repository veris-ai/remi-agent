"""The agent on Cartesia Managed Agents, a hosted orchestrator: Ink STT, the LLM and Sonic TTS run on Cartesia.

Per call the provider creates a managed agent (the session instructions with
the screen pop, the model, the voice, the tools), opens its WebSocket at the
channel's ``pcm_24000`` and relays audio both ways; the agent is **deleted
when the call ends**, however it ends.

Tools are Cartesia **client tools**: the model's call arrives on the agent's
own WebSocket as ``client_tool_call``, runs through the call's
``CallSession.dispatch`` and goes back as ``client_tool_result``. No webhook,
so no public endpoint and nothing to authenticate (the socket is the pod's
own, opened with its API key). Client tools are account-wide resources (an
account holds at most 100, an agent references at most 32), so they are
shared, not made per call: the pod reuses a tool whose stored definition is
identical to the one it needs and creates only what is missing. Two pods never
fight over a definition, and parallel calls never exhaust the account.
Cartesia's parameter schema accepts only ``type``, ``description``, ``enum``,
``items``, ``properties`` and ``required`` (anything else is "Unrecognized
key"), so bounds and formats are cut; ``CallSession.dispatch`` still
validates every argument and returns any violation to the model.

Call start: ``initial_message`` is null, Cartesia's native "wait for the
caller to speak first". Cartesia has no way to ask the model for an opening
turn, so an agent that speaks first cannot run here (``opens_calls = False``;
startup fails with a message). Turn taking, interruptions (``audio_output_clear``)
and noise suppression are Cartesia's own defaults. The LLM catalog has no
``gpt-4.1-mini``; ``gpt-4.1`` is the default, as on the VAmoS Energy candidate.
Tool results over 4096 bytes are replaced by Cartesia with an error. Cartesia
reserves the names of its system tools (``end_call``, ``send_dtmf``,
``voicemail_detection``, ``transfer_to_number``), so a tool with one of those
names is declared with a trailing underscore (Remi's ``end_call`` is
``end_call_`` at Cartesia) and mapped back before dispatch; the system tools
stay off, so the agent's own hang-up tool, with its gates, is the only one.

Trace: ``trace_adapter = "cartesia"`` reads this socket (``client_tool_call``,
``turn_output_text_delta`` and ``turn_ended`` received; ``client_tool_result``
sent), so it is opened with ``wire.connect_websocket``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx
import websockets

from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.hosted import hang_up_after_last_words
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

API_BASE = "https://api.cartesia.ai"
WS_BASE = "wss://api.cartesia.ai"
API_VERSION = "2026-08-14"
API_TIMEOUT_S = 30.0
AUDIO_FORMAT = "pcm_24000"
#: How long Cartesia waits for a client tool's result.
TOOL_TIMEOUT_S = 60
#: Cartesia replaces a longer result with an error.
MAX_RESULT_BYTES = 4096
#: The parameter-schema keywords Cartesia accepts.
_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required"}


@dataclass(frozen=True)
class CartesiaConfig:
    """Model and voice (defaults: the VAmoS Energy Cartesia candidate)."""

    model: str = "gpt-4.1"
    #: "Skylar - Friendly Guide".
    voice_id: str = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CartesiaConfig:
        return cls(
            model=env.get("VAMOS_CARTESIA_MODEL") or cls.model,
            voice_id=env.get("VAMOS_CARTESIA_VOICE_ID") or cls.voice_id,
        )


def _schema(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            out[key] = {name: _schema(sub) for name, sub in value.items()}
        elif key == "items":
            out[key] = _schema(value)
        elif key in _SCHEMA_KEYS:
            out[key] = value
    return out


#: Names Cartesia keeps for its own system tools ("tool_name_reserved").
RESERVED_TOOL_NAMES = frozenset({"end_call", "send_dtmf", "voicemail_detection", "transfer_to_number"})


def wire_name(name: str) -> str:
    """The name a tool is declared under at Cartesia: a reserved name gets a trailing underscore."""
    return f"{name}_" if name in RESERVED_TOOL_NAMES else name


def tool_name(wire: str) -> str:
    """The agent's tool name for a name Cartesia called."""
    base = wire.removesuffix("_")
    return base if wire.endswith("_") and base in RESERVED_TOOL_NAMES else wire


class CartesiaToolFormat:
    """One client tool definition (``POST /v1/agents/tools``) per tool, under its ``wire_name``."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "client",
                "name": wire_name(t.name),
                "description": t.description,
                "pre_tool_speech": "auto",
                "execution_mode": "immediate",
                "expects_response": True,
                "response_timeout_secs": TOOL_TIMEOUT_S,
                "parameters": _schema(t.parameters),
            }
            for t in tools
        ]


#: The fields that make two tool definitions the same tool.
_DEFINITION_KEYS = (
    "type",
    "name",
    "description",
    "pre_tool_speech",
    "execution_mode",
    "expects_response",
    "response_timeout_secs",
    "parameters",
)


def fingerprint(tool: Mapping[str, Any]) -> str:
    return json.dumps({k: tool.get(k) for k in _DEFINITION_KEYS}, sort_keys=True)


class CartesiaProvider(VoiceProvider):
    name = "cartesia"
    trace_adapter = "cartesia"
    required_env = ("CARTESIA_API_KEY",)
    opens_calls = False
    tool_format = CartesiaToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, config: CartesiaConfig | None = None) -> None:
        self._api_key = api_key
        self.config = config or CartesiaConfig()
        #: Tool definition fingerprint -> tool id, filled from the account on first use.
        self._tool_ids: dict[str, str] | None = None
        #: The Cartesia REST client for one call; tests swap in an ``httpx.MockTransport``.
        self.http: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
            base_url=API_BASE,
            timeout=API_TIMEOUT_S,
            headers={"X-API-Key": self._api_key, "Cartesia-Version": API_VERSION},
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(env["CARTESIA_API_KEY"], CartesiaConfig.from_env(env))

    def agent_config(self, setup: SessionSetup, tool_ids: list[str]) -> dict[str, Any]:
        """The managed agent for one call (the parity tests read it back)."""
        return {
            "instructions": setup.instructions,
            "initial_message": None,  # the caller speaks first
            "model": {"id": self.config.model},
            "language": {"primary": "en"},
            "audio": {"output": {"voice_id": self.config.voice_id}},
            "tools": [{"id": tool_id} for tool_id in tool_ids],
        }

    # --- vendor resources ------------------------------------------------------------------

    async def tool_ids(self, api: httpx.AsyncClient, tools: list[ToolSpec]) -> list[str]:
        """The ids of client tools identical to ``tools``: reused from the account, created if missing."""
        if self._tool_ids is None:
            self._tool_ids = await _existing_client_tools(api)
        ids = []
        for definition in self.tool_format.convert(tools):
            key = fingerprint(definition)
            if key not in self._tool_ids:
                response = await api.post("/v1/agents/tools", json=definition)
                _raise_for(response, f"create tool {definition['name']}")
                self._tool_ids[key] = response.json()["id"]
                logger.info("cartesia: created client tool %s", definition["name"])
            ids.append(self._tool_ids[key])
        return ids

    async def run_call(self, call: CallRuntime) -> None:
        async with self.http() as api:
            tool_ids = await self.tool_ids(api, call.setup.tools)
            response = await api.post(
                "/v1/agents", json={"name": "vamos-call", "config": self.agent_config(call.setup, tool_ids)}
            )
            _raise_for(response, "create agent")
            agent_id = response.json()["id"]
            try:
                url = f"{WS_BASE}/v1/agents/websocket/{agent_id}?cartesia_version={API_VERSION}"
                async with self.connect(url, {"X-API-Key": self._api_key}) as ws:
                    await _CartesiaCall(ws, call).run()
            finally:
                deleted = await api.delete(f"/v1/agents/{agent_id}")
                if deleted.is_error and deleted.status_code != 404:
                    logger.error("cartesia: agent %s not deleted: HTTP %s", agent_id, deleted.status_code)


async def _existing_client_tools(api: httpx.AsyncClient) -> dict[str, str]:
    found: dict[str, str] = {}
    params: dict[str, Any] = {"type": "client", "limit": 100}
    while True:
        response = await api.get("/v1/agents/tools", params=params)
        _raise_for(response, "list tools")
        page = response.json()
        for tool in page.get("data") or []:
            found.setdefault(fingerprint(tool), tool["id"])
        if not page.get("has_more") or not page.get("data"):
            return found
        params["starting_after"] = page["data"][-1]["id"]


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.is_error:
        raise RuntimeError(f"Cartesia could not {what}: HTTP {response.status_code} {response.text[:500]}")


class _CartesiaCall:
    def __init__(self, ws: Any, call: CallRuntime) -> None:
        self.ws, self.call = ws, call
        self.turn = 0
        self.ended = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def run(self) -> None:
        await self.send(
            {
                "type": "session_create",
                "audio": {"input_format": AUDIO_FORMAT, "output_delivery": "speaking_pace"},
            }
        )
        ready = json.loads(await self.ws.recv())
        if ready.get("type") != "session_ready":
            raise RuntimeError(f"Cartesia refused the session: {ready}")
        logger.info("cartesia session ready: call %s", ready.get("call_id"))

        async def forward(pcm: bytes) -> None:
            await self.send({"type": "audio_input", "audio": base64.b64encode(pcm).decode()})

        self.call.inbound.bind(forward)
        await run_until_first_done(
            caller_to_vendor=self.call.inbound.run(),
            vendor_to_caller=self.events(),
            agent_hang_up=self.hang_up_when_ended(),
        )

    async def hang_up_when_ended(self) -> None:
        await self.ended.wait()
        await hang_up_after_last_words(self.call)

    async def events(self) -> None:
        try:
            while True:
                await self.on_event(json.loads(await self.ws.recv()))
        except websockets.ConnectionClosed as closed:
            logger.info("cartesia socket closed: %s", closed)

    async def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "audio_output":
            await self.call.outbound.send(base64.b64decode(event["audio"]), f"turn{self.turn}")
        elif kind == "audio_output_clear":
            self.call.outbound.interrupted()
        elif kind == "turn_started" and event.get("role") == "assistant":
            self.turn = int(event.get("turn") or self.turn + 1)
        elif kind == "turn_ended" and event.get("role") == "user" and (event.get("text") or "").strip():
            self.call.start.mark_callee_spoke()
        elif kind == "client_tool_call":
            await self.run_tool(event)
        elif kind == "error":
            if event.get("fatal"):
                raise RuntimeError(f"Cartesia session error: {event}")
            logger.warning("cartesia: %s", event)

    async def run_tool(self, event: dict[str, Any]) -> None:
        # Shielded: a payment that has started must finish even if the caller barges in.
        result = await asyncio.shield(
            self.call.session.dispatch(
                tool_name(event.get("tool_name", "")), dict(event.get("parameters") or {})
            )
        )
        if self.call.session.ended:
            self.ended.set()
        if not event.get("expects_response", True):
            return
        text = json.dumps(result, default=str)
        if len(text.encode()) > MAX_RESULT_BYTES:
            logger.warning(
                "cartesia: %s result is %d bytes (limit %d)",
                event.get("tool_name"),
                len(text),
                MAX_RESULT_BYTES,
            )
        await self.send(
            {
                "type": "client_tool_result",
                "tool_call_id": event.get("tool_call_id"),
                "result": text,
                "is_error": "error" in result,
            }
        )


PROVIDER = CartesiaProvider
