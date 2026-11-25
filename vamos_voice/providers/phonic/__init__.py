"""The agent on Phonic's speech-to-speech API (``wss://api.phonic.ai/v1/sts/ws``).

Phonic runs speech recognition, the model and speech synthesis on its side, over
its own websocket protocol (not OpenAI Realtime-compatible; Phonic's AsyncAPI
spec, ``docs.phonic.ai/asyncapi.json``). Bench candidate: ``trace_adapter:
phonic``, which reads ``config``, ``input_text``, ``audio_chunk``, ``tool_call``,
``tool_call_output``, ``interrupted_response`` and ``assistant_finished_speaking``
off the ``websockets`` client this provider uses.

The session, as in the VAmoS Energy Phonic candidates (rory-agent ``phonic/``):

- one ``config`` first (Phonic sends nothing before it), answered by
  ``ready_to_start_conversation``. It carries the instructions as
  ``system_prompt``, the tools as inline ``custom_websocket`` tools, the model as
  ``phonic_model`` at ``intelligence_level: high``, the voice, and Phonic's
  built-in ``choose_not_to_respond`` (3 s). ``phonic_model`` is deprecated in
  favour of ``model``, but it is the field the bench's ``phonic`` trace adapter
  reads, and the one the Energy candidates send;
- ``pcm_24000`` both ways, base64 in ``audio_chunk``: no resampling. Phonic
  takes at most 40 ms per chunk, so caller audio goes up in 20 ms chunks. Agent
  audio is paced to real time by Phonic (``stream_ahead_of_real_time`` off);
- ``tool_call`` (``parameters`` already an object) is answered with
  ``tool_call_output`` on the same ``tool_call_id``. Calls run in order on their
  own task, so audio and interruptions keep flowing while one runs.

Tools: every tool is ``sync`` and ``uninterruptible``. By default caller speech
cancels a sync call in flight, and a call that has already acted (a payment, a
flag) is then asked for again and recorded twice; ``uninterruptible`` holds the
agent's turn open until the result arrives instead. The Energy candidates make
the five account-changing tools ``async`` + ``wait_for_response`` for the same
reason; a provider cannot know which of an agent's tools write, so here none can
be cancelled. Output timeout 30 s (Phonic's default is 5 s).

Turn taking is Phonic's own (section 10 of AGENTS.md): no VAD setting is sent.
The Energy candidates pin ``vad_min_silence_duration_ms`` to 800; Remi's vendors
all run on their own defaults. On ``interrupted_response`` Phonic stops its own
reply; it keeps in its context what it had said, so nothing is dropped here.

Call start: ``CALLEE_FIRST`` sends no ``welcome_message`` (and
``generate_welcome_message`` stays off), so the agent says nothing until the
callee's first ``input_text``, which opens the outbound gate. ``AGENT_FIRST``:
``generate_reply`` with the opening instruction as its ``system_message``.

Hang-up: once ``end_call`` has run, the line closes after the agent's audio has
gone quiet and the last words have played (``hosted.hang_up_after_last_words``).
Phonic ending the conversation itself (``assistant_ended_conversation``) hangs
up the same way. A close from Phonic with any code but 1000 fails the call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Mapping
from typing import Any, Self

import websockets

from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.hosted import hang_up_after_last_words
from vamos_voice.providers.wire import Connect, connect_websocket, run_until_first_done
from vamos_voice.tools import ToolSpec

logger = logging.getLogger(__name__)

URL = "wss://api.phonic.ai/v1/sts/ws"
DEFAULT_MODEL = "phonic-1.1"
DEFAULT_VOICE = "sabrina"
#: The channel's PCM16 at 24 kHz, both legs.
AUDIO_FORMAT = "pcm_24000"
#: 20 ms of 24 kHz PCM16: Phonic rejects an ``audio_chunk`` over 40 ms.
CHUNK_BYTES = 960
#: Phonic's default ``tool_call_output_timeout_ms`` is 5 s; a slow world call can take longer.
TOOL_OUTPUT_TIMEOUT_MS = 30_000
#: Phonic's built-in turn-taking tool, as in the Energy candidates.
CHOOSE_NOT_TO_RESPOND = {
    "type": "built_in",
    "name": "choose_not_to_respond",
    "tool_config": {"respond_after_sec": 3},
}
#: Agent audio carries no per-turn id on this API; playout is tracked per agent turn.
AUDIO_ITEM = "turn"


class PhonicToolFormat:
    """Inline ``custom_websocket`` tools: the OpenAI function shape under ``tool_schema``.

    Phonic refuses an inline tool without ``strict: true`` ("Invalid input" per tool). The
    parameters go unchanged: Phonic accepts optional properties under the flag.
    """

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "custom_websocket",
                "execution_mode": "sync",
                "uninterruptible": True,
                "tool_call_output_timeout_ms": TOOL_OUTPUT_TIMEOUT_MS,
                "tool_schema": {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "strict": True,
                    },
                },
            }
            for t in tools
        ]


class PhonicProvider(VoiceProvider):
    name = "phonic"
    trace_adapter = "phonic"
    required_env = ("PHONIC_API_KEY",)
    tool_format = PhonicToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self._api_key = api_key
        self.model, self.voice = model, voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["PHONIC_API_KEY"],
            model=env.get("VAMOS_PHONIC_MODEL") or DEFAULT_MODEL,
            voice=env.get("VAMOS_PHONIC_VOICE") or DEFAULT_VOICE,
        )

    def config(self, setup: SessionSetup) -> dict[str, Any]:
        """The one ``config`` message. No welcome message: an opening is requested only for an
        agent that speaks first, and then through the model (``generate_reply``)."""
        return {
            "type": "config",
            "system_prompt": setup.instructions,
            "phonic_model": self.model,
            "intelligence_level": "high",
            "voice_id": self.voice,
            "default_language": "en",
            "input_format": AUDIO_FORMAT,
            "output_format": AUDIO_FORMAT,
            "tools": [*self.tool_format.convert(setup.tools), CHOOSE_NOT_TO_RESPOND],
        }

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(URL, {"Authorization": f"Bearer {self._api_key}"}) as ws:
            await _PhonicCall(self, ws, call).run()


class _PhonicCall:
    """One call's Phonic conversation: the event loop, the tool worker and their state."""

    def __init__(self, provider: PhonicProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.turn = 0
        self.tool_calls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.hanging_up = False

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> dict[str, Any] | None:
        """The next event, or None once Phonic closed normally. Any other close (a 4xxx code such as
        4505 at the duration cap, or no close frame at all) fails the call."""
        try:
            message = await self.ws.recv()
        except websockets.ConnectionClosedOK as closed:
            logger.info("phonic socket closed: %s", closed)
            return None
        except websockets.ConnectionClosedError as closed:
            raise RuntimeError(f"Phonic closed the conversation abnormally: {closed}") from closed
        return json.loads(message)

    async def run(self) -> None:
        call = self.call
        await self.send(self.provider.config(call.setup))
        ready = await self.recv()
        if ready is None:
            raise RuntimeError("Phonic closed the conversation before it was ready")
        if ready.get("type") == "error":
            raise RuntimeError(f"phonic error before the conversation started: {_describe(ready)}")
        if ready.get("type") != "ready_to_start_conversation":
            raise RuntimeError(f"Phonic answered the config with {ready.get('type')!r}")

        async def forward(pcm: bytes) -> None:
            for i in range(0, len(pcm), CHUNK_BYTES):
                await self.send(
                    {"type": "audio_chunk", "audio": base64.b64encode(pcm[i : i + CHUNK_BYTES]).decode()}
                )

        if call.setup.start_policy.agent_opens:
            await self.send(
                {"type": "generate_reply", "system_message": call.setup.start_policy.opening_instruction}
            )
        call.inbound.bind(forward)
        await run_until_first_done(
            caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events(), tools=self.run_tools()
        )

    async def events(self) -> None:
        while (event := await self.recv()) is not None:
            if await self.on_event(event):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "audio_chunk":
            if audio := event.get("audio"):
                await call.outbound.send(base64.b64decode(audio), f"{AUDIO_ITEM}{self.turn}")
        elif kind == "input_text":
            if (event.get("text") or "").strip():
                call.start.mark_callee_spoke()
        elif kind == "interrupted_response":
            call.outbound.interrupted()  # Phonic stops its own reply and keeps it in context
            self.turn += 1
        elif kind == "assistant_finished_speaking":
            self.turn += 1
        elif kind == "tool_call":
            await self.tool_calls.put(event)
        elif kind == "tool_call_interrupted":
            # Not expected: every tool is uninterruptible. The worker still answers the call: a tool
            # that has run has acted.
            logger.warning(
                "phonic interrupted tool %s (%s)", event.get("tool_name"), event.get("tool_call_id")
            )
        elif kind == "conversation_created":
            logger.info("phonic conversation %s", event.get("conversation_id"))
        elif kind == "assistant_ended_conversation":
            logger.info("phonic ended the conversation")
            return await self.hang_up()
        elif kind == "error":
            raise RuntimeError(f"phonic error: {_describe(event)}")
        elif kind == "warning":
            logger.warning("phonic warning: %s", event.get("warning"))
        return False

    async def run_tools(self) -> None:
        """Answer Phonic's tool calls in order (they share the call's state); hang up after ``end_call``."""
        while True:
            event = await self.tool_calls.get()
            result = await self.call.session.dispatch(event["tool_name"], event.get("parameters") or {})
            await self.send(
                {
                    "type": "tool_call_output",
                    "tool_call_id": event["tool_call_id"],
                    "output": _jsonable(result),
                }
            )
            if self.call.session.ended:
                await self.hang_up()
                return

    async def hang_up(self) -> bool:
        if not self.hanging_up:
            self.hanging_up = True
            await hang_up_after_last_words(self.call)
        return True


def _jsonable(result: dict[str, Any]) -> Any:
    """The tool's dict as plain JSON (dates and decimals as strings), as the other providers send it."""
    return json.loads(json.dumps(result, default=str))


def _describe(event: dict[str, Any]) -> str:
    error = event.get("error")
    detail = json.dumps(error) if isinstance(error, dict) else str(error)
    if params := event.get("param_errors"):
        detail += f" {json.dumps(params)}"
    return detail


PROVIDER = PhonicProvider
