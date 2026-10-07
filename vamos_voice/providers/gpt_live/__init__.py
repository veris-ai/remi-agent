"""The agent on OpenAI's GPT-Live API (``wss://api.openai.com/v1/live/sessions``), full duplex.

GPT-Live is not the Realtime protocol with a new model id, so this is its own
provider rather than a ``RealtimeProtocolProvider`` subclass; it shares the
pieces that are genuinely common (the traced ``websockets`` client, the loop
runner, the JSON tool dispatch, the flat function-tool format). Bench
candidate: ``trace_adapter: gpt-live``, which reads exactly the frames below.

The protocol (developers.openai.com "Live conversations", "Live delegation"):

- ``session.start`` carries the whole configuration once: ``model``,
  ``instructions``, ``audio`` (format, voice) and ``delegation``. Only
  ``delegation`` can change later (``session.update``). Audio may be sent only
  after ``session.started``.
- The live model listens and speaks at the same time and stops itself when the
  caller talks over it. There is no VAD to configure, no input buffer to
  commit, no response to cancel and no item to truncate.
- It runs no tools of its own: tool use is delegated to a Responses backend
  (``delegation.responses``: model, instructions, tools, ``tool_choice``). A
  backend run arrives as ``response.event`` envelopes; a function call is the
  nested ``response.output_item.done`` with a ``function_call`` item, the run
  ends at the nested ``response.completed``. Results go back as
  ``response.item.create`` (``function_call_output``), then ``response.create``
  continues the backend.
- There is no ``response.create`` for the live model itself: an application
  makes it act with ``session.instructions.append`` (a trusted instruction,
  not a user turn, so nothing has to be labelled).

Call start (``CALLEE_FIRST``): the session starts with the backend's
``tool_choice: "required"`` and the caller's audio held. An instruction append
(the policy's ``prime_instruction``) makes the live model delegate; the
backend must answer with a tool call. Each read still missing is then forced
by name via ``session.update`` before the backend continues; once all are in,
``tool_choice`` goes back to ``"auto"``, the held audio is released, and no
agent audio leaves before the callee's first transcribed words.

Call start (``AGENT_FIRST``): after ``session.started`` (and any connect
reads), an instruction append carrying the policy's ``opening_instruction``.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any, Self

import websockets

from vamos_voice.providers.base import CallRuntime, SessionSetup, VoiceProvider
from vamos_voice.providers.realtime_protocol import RATE_HZ, RealtimeToolFormat
from vamos_voice.providers.wire import Connect, connect_websocket, dispatch_json_call, run_until_first_done

logger = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/live/sessions"
DEFAULT_MODEL = "gpt-live-1"
#: The Responses model tool use is delegated to (the one the VAmoS Energy GPT-Live candidate ran).
DEFAULT_BACKEND_MODEL = "gpt-5.6-terra"
DEFAULT_VOICE = "marin"
#: Live audio has no per-turn item; playout is tracked under one id.
AUDIO_ITEM = "live"

#: The backend never hears the call: it reads the transcript the live model hands it.
#: Protocol mechanics only; the agent's own instructions follow verbatim.
BACKEND_PREAMBLE = (
    "You are the reasoning and tool-use backend of a live voice agent. You read a transcript of the "
    "call, which can contain recognition mistakes, unfinished phrases and later corrections; use the "
    "latest context and the tool results you receive. Report an action as complete only after its "
    "tool confirms it. The agent's instructions follow.\n\n"
)


def tool_choice_update(choice: Any) -> dict[str, Any]:
    """``session.update`` changing only the backend's ``tool_choice`` (the one mutable part)."""
    return {
        "type": "session.update",
        "session": {"delegation": {"type": "responses", "responses": {"tool_choice": choice}}},
    }


def instruction(content: str) -> dict[str, Any]:
    """``session.instructions.append`` for the whole session (not one delegation)."""
    return {"type": "session.instructions.append", "delegation_id": None, "content": content}


class GptLiveProvider(VoiceProvider):
    name = "gpt_live"
    trace_adapter = "gpt-live"
    required_env = ("OPENAI_API_KEY",)
    #: Responses function tools take the same flat shape as Realtime ones.
    tool_format = RealtimeToolFormat()
    connect: Connect = staticmethod(connect_websocket)

    def __init__(self, api_key: str, model: str, backend_model: str, voice: str) -> None:
        self._api_key = api_key
        self.model, self.backend_model, self.voice = model, backend_model, voice

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls(
            api_key=env["OPENAI_API_KEY"],
            model=env.get("VAMOS_GPT_LIVE_MODEL") or DEFAULT_MODEL,
            backend_model=env.get("VAMOS_GPT_LIVE_BACKEND_MODEL") or DEFAULT_BACKEND_MODEL,
            voice=env.get("VAMOS_GPT_LIVE_VOICE") or DEFAULT_VOICE,
        )

    def session_start(self, setup: SessionSetup, force_tools: bool) -> dict[str, Any]:
        """The one ``session.start``; ``force_tools`` while connect reads are still to run."""
        return {
            "type": "session.start",
            "session": {
                "model": self.model,
                "instructions": setup.instructions,
                "audio": {"format": {"type": "audio/pcm", "rate": RATE_HZ}, "output": {"voice": self.voice}},
                "delegation": {
                    "type": "responses",
                    "responses": {
                        "model": self.backend_model,
                        "instructions": BACKEND_PREAMBLE + setup.instructions,
                        "tools": self.tool_format.convert(setup.tools),
                        "tool_choice": "required" if force_tools else "auto",
                        # One call per run, answered before the next: a call may depend on the last result.
                        "parallel_tool_calls": False,
                    },
                },
            },
        }

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(URL, {"Authorization": f"Bearer {self._api_key}"}) as ws:
            await _LiveCall(self, ws, call).run()


class _LiveCall:
    """One call's GPT-Live session: the event loop and its per-call state."""

    def __init__(self, provider: GptLiveProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.priming = True
        #: Completed function calls per delegation, answered when its backend run completes.
        self.pending: dict[str, list[dict[str, Any]]] = {}

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def recv(self) -> dict[str, Any] | None:
        try:
            raw = await self.ws.recv()
        except websockets.ConnectionClosed as closed:
            logger.info("gpt-live socket closed: %s", closed)
            return None
        return json.loads(raw)

    async def run(self) -> None:
        call = self.call
        missing = call.session.missing_reads
        await self.send(self.provider.session_start(call.setup, force_tools=bool(missing)))
        while (event := await self.recv()) is not None and event.get("type") != "session.started":
            if event.get("type") == "error":
                raise RuntimeError(f"gpt-live error before session.started: {event.get('error')}")
        if event is None:
            raise RuntimeError("GPT-Live closed the session before session.started")

        async def forward(pcm: bytes) -> None:
            await self.send({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode()})

        call.inbound.bind(forward)
        if missing:
            await self.send(instruction(call.setup.start_policy.prime_instruction))
        else:
            await self.finish_priming()
        await run_until_first_done(caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events())

    async def events(self) -> None:
        while (event := await self.recv()) is not None:
            if await self.on_event(event):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "session.output_audio.delta":
            await call.outbound.send(base64.b64decode(event.get("delta", "")), AUDIO_ITEM)
        elif kind == "session.input_transcript.delta":
            if event.get("delta", "").strip() and call.start.primed:
                call.start.mark_callee_spoke()
        elif kind == "response.event":
            return await self.on_backend_event(event.get("delegation_id") or "", event.get("event") or {})
        elif kind == "session.closed":
            logger.info("gpt-live: session.closed reason=%s", event.get("reason"))
            return True
        elif kind == "error":
            raise RuntimeError(f"gpt-live error: {event.get('error')}")
        return False

    async def on_backend_event(self, delegation_id: str, event: dict[str, Any]) -> bool:
        kind = event.get("type", "")
        if kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self.pending.setdefault(delegation_id, []).append(item)
        elif kind == "response.completed":
            return await self.run_completed(self.pending.pop(delegation_id, []))
        elif kind == "error":
            raise RuntimeError(f"gpt-live backend error: {event}")
        return False

    async def run_completed(self, calls: list[dict[str, Any]]) -> bool:
        """A backend run finished: answer its calls, then continue the backend (forcing reads while priming)."""
        call = self.call
        for item in calls:
            output = await dispatch_json_call(call.session, item["name"], item.get("arguments"))
            if output is not None:
                await self.send(
                    {
                        "type": "response.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": item["call_id"],
                            "output": output,
                        },
                    }
                )
        if call.session.ended:
            await call.hang_up()
            await self.send({"type": "session.close"})
            return True
        if self.priming:
            if missing := call.session.missing_reads:
                if not calls:  # the live model answered without delegating: ask again
                    await self.send(instruction(call.setup.start_policy.reprime_instruction(missing)))
                    return False
                await self.send(tool_choice_update({"type": "function", "name": missing[0]}))
                await self.send({"type": "response.create"})
                return False
            await self.send(tool_choice_update("auto"))
            if calls:
                await self.send({"type": "response.create"})
            await self.finish_priming()
            return False
        if calls:
            await self.send({"type": "response.create"})
        return False

    async def finish_priming(self) -> None:
        """The reads are done (or there were none): release the caller, and open if the agent speaks first."""
        call = self.call
        self.priming = False
        call.start.mark_primed()
        await call.inbound.release()
        if call.setup.start_policy.agent_opens:
            await self.send(instruction(call.setup.start_policy.opening_instruction))


PROVIDER = GptLiveProvider
