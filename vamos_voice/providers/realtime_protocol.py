"""The OpenAI Realtime wire protocol (GA), shared by every vendor that speaks it.

OpenAI Realtime speaks it natively; xAI's Grok Voice Agent API speaks a close
dialect. ``RealtimeProtocolProvider`` implements the protocol once; a vendor
subclasses it and supplies ``url``, ``headers`` and ``session_config``, plus
the capability dialects differ on, declared rather than branched on inside
the loop: ``truncates_on_barge_in``, whether ``conversation.item.truncate`` is
sent when the caller talks over the agent (OpenAI yes; Grok documents it as
unsupported).

See ``vamos_voice/providers/openai_realtime`` and ``grok_voice``.

The traffic goes through the ``websockets`` asyncio client, sending text JSON
frames and reading them with ``recv()``, because the bench's
``openai-realtime`` trace adapter builds the judge's agent trace by hooking
exactly that client's ``send``/``recv``. Input transcription is enabled so
the caller's words appear in that trace.

Call start: the opening ``session.update`` carries the instructions with the
agent's screen pop already in them, so the model knows the account before it
hears anything. Caller audio flows to the vendor straight after it.
``CALLEE_FIRST``: nothing more; the vendor's VAD answers the callee's
"Hello?". ``AGENT_FIRST``: one ``response.create`` asks for the opening turn
(the session instructions plus the policy's opening instruction), and its
audio goes straight out.

Turns are the vendor's own: its server VAD decides when the caller has
finished (and the vendor replies), and when the caller interrupts the agent
(the vendor stops its response; this provider only truncates the context to
what was played). Nothing here filters, gates or second-guesses caller audio:
these agents are benchmark reference candidates, and background noise is a
benchmark condition that measures the vendor.

The provider's own requests are serialized: a ``response.create`` it asks for
(tool results, the opening turn) while another response is in flight waits
for it and goes out at its ``response.done`` (several requests coalesce into
one), and one refused with ``conversation_already_has_active_response`` is
sent again when the running response ends.
"""

from __future__ import annotations

import abc
import base64
import json
import logging
from collections import deque
from typing import Any, ClassVar

import websockets

from ..tools import ToolSpec
from .base import CallRuntime, SessionSetup, VoiceProvider
from .wire import Connect, connect_websocket, dispatch_json_call, run_until_first_done

logger = logging.getLogger(__name__)

RATE_HZ = 24_000

#: Error codes that mean "that request lost a race", never a broken session.
BENIGN_ERRORS = ("conversation_already_has_active_response", "response_cancel_not_active")

RESPONSE_CREATE: dict[str, Any] = {"type": "response.create"}


class RealtimeToolFormat:
    """Realtime function tools: ``{"type": "function", "name", "description", "parameters"}``."""

    def convert(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ]


class RealtimeProtocolProvider(VoiceProvider, abc.ABC):
    """A vendor speaking the Realtime protocol. Subclasses give the endpoint and session."""

    tool_format = RealtimeToolFormat()
    #: Send ``conversation.item.truncate`` on barge-in, so the model's context holds only what was heard.
    truncates_on_barge_in: ClassVar[bool] = True
    #: Overridable in tests to capture the outbound frames without a network.
    connect: Connect = staticmethod(connect_websocket)

    @abc.abstractmethod
    def url(self) -> str: ...

    @abc.abstractmethod
    def headers(self) -> dict[str, str]: ...

    @abc.abstractmethod
    def session_config(self, setup: SessionSetup) -> dict[str, Any]:
        """The ``session`` object of the opening ``session.update``."""

    def opening_response(self, setup: SessionSetup) -> dict[str, Any]:
        """The ``response.create`` that makes the agent speak first (AGENT_FIRST)."""
        instructions = f"{setup.instructions}\n\n{setup.start_policy.opening_instruction}"
        return {"type": "response.create", "response": {"instructions": instructions}}

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(self.url(), self.headers()) as ws:
            await _RealtimeCall(self, ws, call).run()


class _RealtimeCall:
    """One call's Realtime session: the event loop and its per-call state."""

    def __init__(self, provider: RealtimeProtocolProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.tool_answered = False  # a function_call_output went out in the current response
        self.audio_item: str | None = None
        # Responses this provider asked for, oldest first (popped at response.created), what
        # the running one is for ("vendor" when the vendor's VAD started it), and the one
        # request waiting for it to finish.
        self.requested: deque[tuple[dict[str, Any], str]] = deque()
        self.active: str | None = None
        self.pending: tuple[dict[str, Any], str] | None = None

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def run(self) -> None:
        call = self.call
        await self.send({"type": "session.update", "session": self.provider.session_config(call.setup)})
        call.inbound.bind(self.forward)
        if call.setup.start_policy.agent_opens:
            await self.request_response(self.provider.opening_response(call.setup), "opening")
        await run_until_first_done(caller_to_vendor=call.inbound.run(), vendor_to_caller=self.events())

    async def forward(self, pcm: bytes) -> None:
        """Caller audio to the vendor, unchanged: turn detection is the vendor's."""
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    async def events(self) -> None:
        while True:
            try:
                raw = await self.ws.recv()
            except websockets.ConnectionClosed as closed:
                logger.info("realtime socket closed: %s", closed)
                return
            event = json.loads(raw)
            if await self.on_event(event):
                return

    async def on_event(self, event: dict[str, Any]) -> bool:
        """Handle one server event; True when the call is over."""
        kind = event.get("type", "")
        call = self.call
        if kind == "response.output_audio.delta":
            item = event.get("item_id", "")
            self.audio_item = item
            await call.outbound.send(base64.b64decode(event.get("delta", "")), item)
        elif kind == "response.created":
            self.active = self.requested.popleft()[1] if self.requested else "vendor"
        elif kind == "input_audio_buffer.speech_started":
            await self.barge_in()  # the vendor's VAD stops its own response
        elif kind == "input_audio_buffer.committed":
            call.start.mark_callee_spoke()  # the vendor's VAD answers the turn
        elif kind == "response.function_call_arguments.done":
            await self.run_tool(event)
        elif kind == "response.done":
            return await self.response_done(event.get("response") or {})
        elif kind == "error":
            error = event.get("error") or {}
            if error.get("code") not in BENIGN_ERRORS:
                raise RuntimeError(f"realtime error: {error}")
            logger.info("realtime: benign error %s", error.get("code"))
            if error.get("code") == "conversation_already_has_active_response" and self.requested:
                self.pending = self.pending or self.requested.popleft()
        return False

    # --- responses ---------------------------------------------------------------------------

    async def request_response(self, frame: dict[str, Any], purpose: str) -> None:
        """Ask for a response now, or right after the one in flight (one waiting request at most)."""
        if self.active is not None or self.requested:
            self.pending = self.pending or (frame, purpose)
            return
        self.requested.append((frame, purpose))
        await self.send(frame)

    async def run_tool(self, event: dict[str, Any]) -> None:
        output = await dispatch_json_call(self.call.session, event["name"], event.get("arguments"))
        if output is None:
            return
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": event["call_id"], "output": output},
            }
        )
        self.tool_answered = True

    async def response_done(self, response: dict[str, Any]) -> bool:
        call = self.call
        answered, self.tool_answered = self.tool_answered, False
        if self.active is None and self.requested:
            self.requested.popleft()  # done without a created (never in the GA protocol; be safe)
        self.active = None
        if call.session.ended:
            await call.hang_up()
            return True
        # One follow-up covers both: the tool results and any request that waited for this response.
        follow = self.pending or ((RESPONSE_CREATE, "tool results") if answered else None)
        self.pending = None
        if follow is not None:
            await self.request_response(*follow)
        return False

    async def barge_in(self) -> None:
        """The caller talks over the agent. Where the vendor supports it, tell it how much of the
        reply the caller actually heard, so the model's context does not contain words that never
        played; either way, what is still queued will not be heard."""
        item, outbound = self.audio_item, self.call.outbound
        self.audio_item = None
        if not item or outbound.played_ms(item) >= outbound.sent_ms(item):
            return  # nothing of it is still unplayed
        if not self.provider.truncates_on_barge_in:
            outbound.interrupted()
            return
        await self.send(
            {
                "type": "conversation.item.truncate",
                "item_id": item,
                "content_index": 0,
                "audio_end_ms": outbound.played_ms(item),
            }
        )
        outbound.interrupted()
