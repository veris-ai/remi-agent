"""The OpenAI Realtime wire protocol (GA), shared by every vendor that speaks it.

OpenAI Realtime speaks it natively; xAI's Grok Voice Agent API speaks a close
dialect. ``RealtimeProtocolProvider`` implements the protocol once; a vendor
subclasses it and supplies ``url``, ``headers`` and ``session_config``, plus
the capabilities dialects differ on, each declared rather than branched on
inside the loop:

- ``priming_frames``: how the connect reads are forced. The default is a
  text-only ``response.create`` with ``tool_choice`` and the priming
  instruction (OpenAI); a dialect without forced tool choice (Grok) sends a
  labelled note instead.
- ``truncates_on_barge_in``: whether ``conversation.item.truncate`` is sent
  when the caller talks over the agent (OpenAI yes; Grok documents it as
  unsupported).
- ``manages_turns``: whether this provider, not the vendor's server VAD,
  decides when a caller turn gets a reply and when it interrupts the agent
  (OpenAI yes, with ``create_response``/``interrupt_response`` off; Grok
  documents neither switch nor ``response.cancel``, so its VAD keeps both).

See ``vamos_voice/providers/openai_realtime`` and ``grok_voice``.

The traffic goes through the ``websockets`` asyncio client, sending text JSON
frames and reading them with ``recv()``, because the bench's
``openai-realtime`` trace adapter builds the judge's agent trace by hooking
exactly that client's ``send``/``recv``. Input transcription is enabled so
the caller's words appear in that trace.

Call start (``CALLEE_FIRST``): the session opens with the caller's audio held;
the vendor's ``priming_frames`` make the model run the connect reads (OpenAI:
a text-only ``response.create`` with ``tool_choice: "required"``, then any
skipped read forced by name). Only then is the held audio released, so server
VAD hears "Hello?" after the reads are done and the reply knows the account.
Per-response settings (``output_modalities``, ``tool_choice``,
``instructions``) never touch the session, so the caller's reply is spoken.

Call start (``AGENT_FIRST``): any connect reads are primed the same way, then
one ``response.create`` asks for the opening turn (the session instructions
plus the policy's opening instruction), and its audio goes straight out.

Turns (``manages_turns``). Server VAD cannot tell the caller from a TV behind
them: in bench attempts with background speech, every broadcast phrase became
a caller turn whose ``speech_started`` cancelled the agent's reply within a
second, so the agent never finished a sentence. Here a VAD turn counts as the
caller only when ``CallerSpeech`` heard the caller's own voice in it: only then
does it interrupt the agent (``response.cancel`` + truncate) and get a reply.
A background-only turn stays in the conversation (its transcript is traced)
but is neither answered nor allowed to interrupt.

Responses are serialized: a ``response.create`` asked for while another
response is in flight waits for it and goes out at its ``response.done``
(several requests coalesce into one), so a reply is never lost to
``conversation_already_has_active_response`` or to a cancelled response.

Liveness: once the caller has finished a turn, a reply must start within
``TurnTaking.reply_timeout_s``; otherwise the provider logs a warning and asks
for one, so a call can never sit silent on a lost or stalled response.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
from collections import deque
from typing import Any, ClassVar

import websockets

from ..audio import CallerSpeech
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
    #: The provider decides which VAD turns get a reply and may interrupt (the session must turn the
    #: vendor's ``create_response`` and ``interrupt_response`` off); False leaves both to the vendor.
    manages_turns: ClassVar[bool] = True
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

    def priming_frames(self, setup: SessionSetup, missing: list[str], first: bool) -> list[dict[str, Any]]:
        """What makes the model run the connect reads before the callee is heard.

        ``first`` is the opening request; later ones name the reads still ``missing``.
        Default: a text-only ``response.create`` forcing a tool call (any, then the missing
        one by name), carrying the priming instruction. Text-only, because a priming
        response that may speak can talk for seconds before it calls a read (it did, and
        the priming deadline cut it off with no read run); the setting is per response,
        so the session stays audio.
        """
        policy = setup.start_policy
        choice: Any = "required" if first else {"type": "function", "name": missing[0]}
        note = policy.prime_instruction if first else policy.reprime_instruction(missing)
        return [
            {
                "type": "response.create",
                "response": {
                    "tool_choice": choice,
                    "output_modalities": ["text"],
                    "instructions": f"{setup.instructions}\n\n{note}",
                },
            }
        ]

    async def run_call(self, call: CallRuntime) -> None:
        async with self.connect(self.url(), self.headers()) as ws:
            await _RealtimeCall(self, ws, call).run()


class _RealtimeCall:
    """One call's Realtime session: the event loop and its per-call state."""

    def __init__(self, provider: RealtimeProtocolProvider, ws: Any, call: CallRuntime) -> None:
        self.provider, self.ws, self.call = provider, ws, call
        self.priming = True
        self.tool_answered = False  # a function_call_output went out in the current response
        self.audio_item: str | None = None
        self.speech = CallerSpeech()
        # The caller turn server VAD has open: where it started on the input timeline, and
        # whether the caller's own voice (not background) has been heard in it.
        self.turn_start_ms: int | None = None
        self.turn_end_ms: int | None = None
        self.turn_is_caller = False
        self.last_turn_end_ms = 0
        # Responses: what each requested one is for (popped at response.created), the one
        # running, and the request waiting for it to finish.
        self.requested: deque[str] = deque()
        self.active: str | None = None
        self.pending: tuple[dict[str, Any], str] | None = None
        self.reply_guard: asyncio.Task[None] | None = None

    async def send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    async def run(self) -> None:
        call = self.call
        await self.send({"type": "session.update", "session": self.provider.session_config(call.setup)})
        call.inbound.bind(self.forward)
        if missing := call.session.missing_reads:
            await self.send_priming(missing, first=True)
        else:
            await self.finish_priming()
        try:
            await run_until_first_done(
                call.start.priming_deadline(lambda: call.session.missing_reads, self.finish_priming),
                caller_to_vendor=call.inbound.run(),
                vendor_to_caller=self.events(),
            )
        finally:
            self.disarm_reply_guard()

    async def forward(self, pcm: bytes) -> None:
        """Caller audio to the vendor, measured on the way so a VAD turn can be attributed."""
        self.speech.feed(pcm)
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
        if self.turn_start_ms is not None and not self.turn_is_caller:
            await self.check_caller_in_turn()

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
            self.active = self.requested.popleft() if self.requested else "vendor"
            self.disarm_reply_guard()  # a reply has started
        elif kind == "input_audio_buffer.speech_started":
            await self.turn_started(event)
        elif kind == "input_audio_buffer.speech_stopped":
            self.turn_end_ms = event.get("audio_end_ms")
        elif kind == "input_audio_buffer.committed":
            await self.turn_committed()
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
                self.pending = self.pending or (RESPONSE_CREATE, self.requested.popleft())
        return False

    # --- caller turns ------------------------------------------------------------------------

    async def turn_started(self, event: dict[str, Any]) -> None:
        if not self.provider.manages_turns:
            await self.barge_in()  # the vendor's VAD interrupts on its own
            return
        self.turn_start_ms = event.get("audio_start_ms", self.last_turn_end_ms)
        self.turn_end_ms, self.turn_is_caller = None, False
        await self.check_caller_in_turn()

    async def check_caller_in_turn(self) -> None:
        """The open VAD turn becomes the caller's once their own voice is heard in it: only
        then may it interrupt the agent."""
        assert self.turn_start_ms is not None
        if not self.speech.caller_spoke(self.turn_start_ms):
            return
        self.turn_is_caller = True
        if self.active not in (None, "priming"):
            await self.send({"type": "response.cancel"})
        await self.barge_in()

    async def turn_committed(self) -> None:
        call = self.call
        if not self.provider.manages_turns:
            call.start.mark_callee_spoke()
            if not self.priming:
                self.arm_reply_guard()
            return
        start = self.turn_start_ms if self.turn_start_ms is not None else self.last_turn_end_ms
        is_caller = self.turn_is_caller or self.speech.caller_spoke(start, self.turn_end_ms)
        heard_ms = self.speech.foreground_ms(start, self.turn_end_ms)
        self.last_turn_end_ms = self.turn_end_ms if self.turn_end_ms is not None else self.speech.position_ms
        self.turn_start_ms, self.turn_end_ms, self.turn_is_caller = None, None, False
        if not is_caller:
            logger.info(
                "caller line: a VAD turn with only background audio (%d ms of the caller's voice); not answered",
                heard_ms,
            )
            return
        call.start.mark_callee_spoke()
        if not self.priming:
            await self.request_response(RESPONSE_CREATE, "reply")
            self.arm_reply_guard()

    def arm_reply_guard(self) -> None:
        self.disarm_reply_guard()
        self.reply_guard = asyncio.create_task(self.reply_deadline())

    def disarm_reply_guard(self) -> None:
        if self.reply_guard is not None and self.reply_guard is not asyncio.current_task():
            self.reply_guard.cancel()
        self.reply_guard = None

    async def reply_deadline(self) -> None:
        timeout = self.call.setup.turn_taking.reply_timeout_s
        await asyncio.sleep(timeout)
        if self.call.session.ended or self.call.hung_up:
            return
        logger.warning(
            "liveness: the caller finished a turn %.1fs ago and no reply has started "
            "(response in flight: %s); requesting one",
            timeout,
            self.active or "none",
        )
        self.reply_guard = None
        if self.active is None:
            self.requested.clear()  # asked for, never started: lost
        await self.request_response(RESPONSE_CREATE, "liveness")

    # --- responses ---------------------------------------------------------------------------

    async def request_response(self, frame: dict[str, Any], purpose: str) -> None:
        """Ask for a response now, or right after the one in flight (one waiting request at most)."""
        if self.active is not None or self.requested:
            self.pending = self.pending or (frame, purpose)
            return
        self.requested.append(purpose)
        await self.send(frame)

    async def send_priming(self, missing: list[str], first: bool) -> None:
        for frame in self.provider.priming_frames(self.call.setup, missing, first):
            if frame.get("type") == "response.create":
                self.requested.append("priming")
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
        if self.priming:
            missing = call.session.missing_reads
            if missing:
                await self.send_priming(missing, first=False)
                return False
            await self.finish_priming()
            return False
        if call.session.ended:
            await call.hang_up()
            return True
        # One follow-up covers both: the tool results and any caller turn that waited for this response.
        follow = self.pending or ((RESPONSE_CREATE, "tool results") if answered else None)
        self.pending = None
        if follow is not None:
            await self.request_response(*follow)
        return False

    async def finish_priming(self) -> None:
        """The reads are done (or there were none, or the deadline passed): release the caller,
        and open if the agent speaks first. Runs once. A priming response still in flight
        at the deadline is left to finish (its reads count); a caller turn meanwhile is
        answered right after it."""
        if not self.priming:
            return
        call = self.call
        self.priming = False
        call.start.mark_primed()
        await call.inbound.release()
        if call.setup.start_policy.agent_opens:
            await self.request_response(self.provider.opening_response(call.setup), "opening")

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
