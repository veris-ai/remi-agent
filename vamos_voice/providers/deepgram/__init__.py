"""The agent on the Deepgram Voice Agent API (``wss://agent.deepgram.com/v1/agent/converse``).

One socket carries Deepgram's STT (``nova-3``), a managed LLM (``open_ai``
``gpt-4.1-mini``: no key of ours, the LLM runs on Deepgram's account) and
Deepgram TTS (``aura-2``), as in the VAmoS Energy Deepgram candidate. Bench
candidate: ``trace_adapter: deepgram-voice-agent``, which reads ``Settings``,
``ConversationText``, ``FunctionCallRequest`` and ``FunctionCallResponse`` off
the ``websockets`` client this provider uses.

The protocol (developers.deepgram.com, "Voice Agent API"):

- the socket is mixed: text frames are JSON events, binary frames are audio.
  Both legs are ``linear16`` at the channel's 24 kHz (``container: none`` for
  raw PCM out), so audio passes through untouched;
- ``Settings`` carries the whole session once: the instructions as
  ``agent.think.prompt``, the tools as ``agent.think.functions`` (flat
  ``name``/``description``/``parameters``; no ``endpoint`` makes a function
  client-side), the three providers. Audio sent before ``SettingsApplied`` is
  discarded, so caller audio is bound only after it;
- a client-side call arrives as ``FunctionCallRequest`` (``functions``: ``id``,
  ``name``, ``arguments`` as a JSON string, ``client_side``) and is answered
  with ``FunctionCallResponse`` (``id``, ``name``, ``content`` as a string).

Turn taking is Deepgram's own: the ``nova-3`` listen provider exposes no
endpointing setting in ``Settings``, so ``TurnTaking`` is not mapped (as on
GPT-Live). There is no truncate: on ``UserStartedSpeaking`` Deepgram stops its
own reply; this provider only marks the playout as interrupted.

Call start: ``CALLEE_FIRST`` sends no ``greeting`` (Deepgram would speak it at
once) and asks nothing of the model; the first ``ConversationText`` from the
user opens the outbound audio gate. ``AGENT_FIRST``: the Voice Agent's only
way to ask for an LLM turn is ``InjectUserMessage``, so the opening goes in as
the policy's labelled ``opening_note`` (as on Gemini Live).

Hang-up: once ``end_call`` has run, the line closes as soon as the agent is
not speaking (at its ``AgentAudioDone`` when it is), after the last words play.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Self

import websockets

from vamos_voice.audio import CHANNEL_RATE_HZ
from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.wire import Connect, connect_websocket, dispatch_json_call, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

URL = "wss://agent.deepgram.com/v1/agent/converse"
DEFAULT_LISTEN_MODEL = "nova-3"
#: The think model of the VAmoS Energy Deepgram candidate, on Deepgram's managed OpenAI access.
DEFAULT_LLM_MODEL = "gpt-4.1-mini"
DEFAULT_VOICE = "aura-2-thalia-en"
#: Agent audio has no per-turn id on this API; playout is tracked per agent turn.
AUDIO_ITEM = "turn"


class DeepgramToolFormat:
    """``agent.think.functions``: flat ``{name, description, parameters}``, no ``endpoint`` (client-side)."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]


class DeepgramProvider(VoiceProvider):
    name = "deepgram"
    trace_adapter = "deepgram-voice-agent"
    required_env = ("DEEPGRAM_API_KEY",)
    tool_format = DeepgramToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, listen_model: str, llm_model: str, voice: str) -> None:
        self._api_key = api_key
        self.listen_model, self.llm_model, self.voice = listen_model, llm_model, voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["DEEPGRAM_API_KEY"],
            listen_model=env.get("VAMOS_DEEPGRAM_LISTEN_MODEL") or DEFAULT_LISTEN_MODEL,
            llm_model=env.get("VAMOS_DEEPGRAM_LLM_MODEL") or DEFAULT_LLM_MODEL,
            voice=env.get("VAMOS_DEEPGRAM_VOICE") or DEFAULT_VOICE,
        )

    def settings(self, setup: SessionSetup) -> dict[str, Any]:
        """The one ``Settings`` message. No ``greeting``: an opening is requested only for an agent
        that speaks first, and then through the model (``InjectUserMessage``)."""
        pcm = {"encoding": "linear16", "sample_rate": CHANNEL_RATE_HZ}
        return {
            "type": "Settings",
            "audio": {"input": pcm, "output": {**pcm, "container": "none"}},
            "agent": {
                "language": "en",
                "listen": {"provider": {"type": "deepgram", "model": self.listen_model}},
                "think": {
                    "provider": {"type": "open_ai", "model": self.llm_model},
                    "prompt": setup.instructions,
                    "functions": self.tool_format.convert(setup.tools),
                },
                "speak": {"provider": {"type": "deepgram", "model": self.voice}},
            },
        }

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(URL, {"Authorization": f"Token {self._api_key}"}) as ws:
            await _DeepgramCall(self, ws, call).run()


class _DeepgramCall:
    """One call's Voice Agent session: the event loop and its per-call state."""

    def __init__(self, provider: DeepgramProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.turn = 0
        self.speaking = False
        self.hanging_up = False

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> str | bytes | None:
        try:
            return await self.ws.recv()
        except websockets.ConnectionClosed as closed:
            logger.info("deepgram socket closed: %s", closed)
            return None

    async def run(self) -> None:
        call = self.call
        await self.send(self.provider.settings(call.setup))
        while (message := await self.recv()) is not None:
            if isinstance(message, bytes):
                continue
            event = json.loads(message)
            if event.get("type") == "SettingsApplied":
                break
            if event.get("type") == "Error":
                raise RuntimeError(f"deepgram error before SettingsApplied: {_describe(event)}")
        if message is None:
            raise RuntimeError("Deepgram closed the session before SettingsApplied")

        async def forward(pcm: bytes) -> None:
            await self.ws.send(pcm)  # binary linear16 at 24 kHz, unchanged

        call.inbound.bind(forward)
        if call.setup.start_policy.agent_opens:
            await self.send({"type": "InjectUserMessage", "content": call.setup.start_policy.opening_note})
        await run_until_first_done(caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events())

    async def events(self) -> None:
        while (message := await self.recv()) is not None:
            if isinstance(message, bytes):
                self.speaking = True
                await self.call.outbound.send(message, f"{AUDIO_ITEM}{self.turn}")
                continue
            if await self.on_event(json.loads(message)):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "ConversationText":
            if event.get("role") == "user" and (event.get("content") or "").strip():
                call.start.mark_callee_spoke()
        elif kind == "UserStartedSpeaking":
            call.outbound.interrupted()  # Deepgram stops its own reply; nothing to truncate
        elif kind == "AgentStartedSpeaking":
            self.speaking = True
        elif kind == "AgentAudioDone":
            self.speaking = False
            self.turn += 1
            if self.hanging_up:
                return await self.hang_up()
        elif kind == "FunctionCallRequest":
            await self.run_tools(event.get("functions") or [])
            if call.session.ended:
                self.hanging_up = True
                if not self.speaking:
                    return await self.hang_up()
        elif kind == "Error":
            raise RuntimeError(f"deepgram error: {_describe(event)}")
        elif kind == "Warning":
            logger.warning("deepgram warning: %s", _describe(event))
        return False

    async def run_tools(self, functions: list[dict[str, Any]]) -> None:
        """Answer each client-side call in order (they share the call's state)."""
        for fn in functions:
            if not fn.get("client_side", True):
                continue  # a server-side function Deepgram runs itself (none are declared)
            output = await dispatch_json_call(self.call.session, fn["name"], fn.get("arguments"))
            if output is None:
                output = json.dumps({"error": "the arguments were cut off; call the tool again"})
            await self.send(
                {"type": "FunctionCallResponse", "id": fn["id"], "name": fn["name"], "content": output}
            )

    async def hang_up(self) -> bool:
        await self.call.hang_up()
        return True


def _describe(event: dict[str, Any]) -> str:
    return f"{event.get('code') or event.get('err_code')}: {event.get('description') or event.get('err_msg')}"


PROVIDER = DeepgramProvider
