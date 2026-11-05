"""The one seam between an agent and a voice vendor.

A ``VoiceProvider`` turns one bench call into one vendor session. It receives
a ``CallRuntime`` holding everything vendor-neutral, already built and shared
by every provider:

- ``setup``: the rendered instructions (the agent's screen pop already
  appended, ``vamos_voice.screen_pop``), the tool specs (convert them with your
  ``ToolFormat``), and the turn-taking numbers every vendor maps to its own VAD;
- ``session``: send every tool call to ``session.dispatch`` and return its dict;
- ``start``: the ``CallStart`` (hold the line for the callee, or open with a greeting);
- ``inbound`` / ``outbound``: caller audio in (forwarded once the provider binds
  its vendor send and runs the pump) and agent audio out (dropped until the
  callee has spoken; then the agent's playback buffer: written at most
  ``PLAYBACK_LEAD_S`` ahead of real time, the rest queued, and what was written
  tracked for barge-in truncation and for hanging up after the last words).

A provider owns only what is genuinely vendor-specific: the wire protocol, the
tool-format conversion, how it requests an opening turn, and how it maps
barge-in. Everything a rule or a grader cares about lives above this line, and
no provider ever imports an agent: it sees only what the ``CallRuntime`` carries.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, Self

from fastapi import APIRouter
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ..audio import pcm_ms
from ..call_start import CallStart, CallStartPolicy
from ..framing import NORMAL_CLOSURE, Framing
from ..session import CallSession
from ..tools import ToolSpec

logger = logging.getLogger(__name__)

#: Longest the agent waits for its last words to play before closing the line.
MAX_DRAIN_S = 15.0
#: How far ahead of real time agent audio is written to the caller's line (the in-pod cascade's and
#: Fluxions' lead). The ``voice_ws`` caller plays every byte it receives, so audio beyond this waits in
#: ``OutboundAudio``, where an interruption can still drop it (only where the vendor's context is cut to match).
PLAYBACK_LEAD_S = 1.0
#: Where a provider's own HTTP routes are mounted: ``/vendor/<provider name>/...``.
VENDOR_ROUTE_PREFIX = "/vendor"
#: Where this pod runs the tool loop (the cascades, LiveKit), the most LLM rounds one caller turn may take,
#: each with its tool calls: far above a normal turn, it only stops a model that loops.
MAX_TOOL_ROUNDS = 10
#: Where this pod makes the vendor requests itself (the cascades' LLM, STT and TTS requests, Pipecat's and
#: LiveKit's LLM), how long a transient error (429, 5xx, a dropped connection) is retried with backoff
#: before the request fails: one budget for every vendor.
RETRY_BUDGET_S = 25.0
#: The ``openai`` SDK's backoff (0.5 s, doubling, capped at 8 s) spends ~23.5 s over this many retries.
OPENAI_SDK_MAX_RETRIES = 6


@dataclass(frozen=True)
class TurnTaking:
    """Local speech detection for the in-pod cascades, the only stacks with no vendor turn taking.

    Never sent to a vendor: every vendor that detects turns (Realtime, Gemini
    Live, Pipecat, LiveKit, Vapi, the managed agents, xAI STT) runs on its own
    defaults. ``silence_ms`` ends a caller turn where the STT leg has no
    endpointing (Mistral's Voxtral Realtime, Whisper on Hugging Face);
    ``vad_threshold`` is Silero's start threshold (its own default) for that
    end of turn and for cascade barge-in.
    """

    silence_ms: int = 800
    vad_threshold: float = 0.5


@dataclass(frozen=True)
class SessionSetup:
    """What every provider sends its vendor at session start."""

    instructions: str
    tools: list[ToolSpec]
    turn_taking: TurnTaking
    start_policy: CallStartPolicy


class ToolFormat(Protocol):
    """Converts vendor-neutral ``ToolSpec``s into a vendor's native tool declarations."""

    def convert(self, tools: list[ToolSpec]) -> Any: ...


class CallerLine:
    """The bench's ``voice_ws`` socket: PCM16 24 kHz mono both ways, in the candidate's ``Framing``.

    ``close()`` is the one hang-up path: whoever ends the call from this side
    (the agent's hang-up tool via ``CallRuntime.hang_up``, a failed provider,
    the server once the call is over) ends it here, so every vendor hangs up
    the same way.
    """

    def __init__(self, websocket: WebSocket, framing: Framing = Framing.BINARY) -> None:
        self.websocket = websocket  # for providers whose framework owns the transport (Pipecat)
        self.framing = framing
        self.closed = False
        #: The caller hung up in-band (``{"type": "end"}``), as opposed to dropping the socket.
        self.caller_ended = False
        #: Called once, at once, when the caller hangs up or the socket drops (the server points it
        #: at ``CallSession.caller_gone``, so the agent's record never waits for vendor teardown).
        self.on_caller_gone: Callable[[], None] | None = None
        self._hung_up = False
        self._gone_reported = False

    async def receive(self) -> bytes | None:
        """The next audio frame, or None once the caller hangs up."""
        while not self.closed:
            try:
                message = await self.websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                break
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            decoded = self.framing.decode(data if data is not None else message.get("text") or "")
            if decoded is None:
                continue
            if decoded.end:
                logger.info("caller hung up (end message)")
                self.caller_ended = True
                break
            return decoded.audio
        self.caller_gone()
        return None

    def caller_gone(self) -> None:
        """The caller is no longer on the line (an ``end`` message, or the socket closed). For a
        provider whose framework reads the socket itself (Pipecat), its transport calls this."""
        self.closed = True
        if not self._gone_reported:
            self._gone_reported = True
            if self.on_caller_gone is not None:
                self.on_caller_gone()

    def _connected(self) -> bool:
        return (
            self.websocket.client_state == WebSocketState.CONNECTED
            and self.websocket.application_state == WebSocketState.CONNECTED
        )

    async def send(self, pcm: bytes) -> None:
        if self.closed or not self._connected():
            return
        payload = self.framing.encode_audio(pcm)
        try:
            if isinstance(payload, bytes):
                await self.websocket.send_bytes(payload)
            else:
                await self.websocket.send_text(payload)
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True

    async def close(self) -> None:
        """Hang up: the framing's end message (json; not echoed to a caller who sent ``end``),
        then a normal (1000) close."""
        if self._hung_up or not self._connected():
            return
        self._hung_up = self.closed = True
        try:
            if not self.caller_ended and (end := self.framing.end_message()) is not None:
                await self.websocket.send_text(end)
            await self.websocket.close(code=NORMAL_CLOSURE)
        except (WebSocketDisconnect, RuntimeError):
            logger.info("caller line was already gone when the agent hung up")


class InboundAudio:
    """Caller audio toward the vendor, in order, once the provider has bound its vendor send."""

    def __init__(self, line: CallerLine, forward: Callable[[bytes], Awaitable[None]] | None = None) -> None:
        self._line = line
        self._forward = forward

    def bind(self, forward: Callable[[bytes], Awaitable[None]]) -> None:
        """Where caller audio goes (the provider's vendor send). Bind before ``run``."""
        self._forward = forward

    async def run(self) -> None:
        """Pump until the caller hangs up."""
        if self._forward is None:
            raise RuntimeError("InboundAudio has no destination: call bind() first")
        while (frame := await self._line.receive()) is not None:
            await self._forward(frame)


@dataclass
class _Item:
    #: Accepted from the vendor: written to the line or still queued.
    n_bytes: int = 0
    #: Written to the line.
    written_bytes: int = 0
    #: When the far end, playing at 1x, reaches the end of this item's audio written so far.
    playout_until: float = 0.0


class OutboundAudio:
    """Agent audio toward the caller: the call-start gate, then the agent's playback buffer.

    The bench's ``voice_ws`` caller plays every byte it receives, at speaking speed, and the
    framing has no message that clears its buffer (``vamos_voice.framing``): whatever is written
    to the line will be heard. Most vendors stream faster than real time. So this is the playback
    buffer a phone gateway would be: audio is written at most ``PLAYBACK_LEAD_S`` ahead of real
    time and the rest waits here in order. What was written is what the caller hears
    (``heard_ms``). The agent's memory must hold exactly what the caller heard, so queued audio is
    dropped on the vendor's interruption (``interrupted(drop_unsent=True)``) only by a provider
    that then cuts the vendor's context to ``heard_ms``; otherwise all of it plays.
    """

    def __init__(
        self,
        line: CallerLine,
        start: CallStart,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._line = line
        self._start = start
        self._monotonic = monotonic
        self._sleep = sleep
        self._playout_until = 0.0
        self._last_sent_at: float | None = None
        self._items: dict[str, _Item] = {}
        self._queue: deque[tuple[bytes, str]] = deque()  # accepted, not yet written: (pcm, item id)
        self._pump: asyncio.Task[None] | None = None
        self.dropped_bytes = 0
        self.sent_bytes = 0

    async def send(self, pcm: bytes, item_id: str = "") -> bool:
        """Accept agent audio (True); False, and dropped, while the line is held for the callee.

        Never waits for playout: providers call this from their vendor event loops, which must keep
        reading (an interruption must be seen at once). With nothing queued and the line within the
        lead of real time, the audio is written at once; otherwise it is queued behind what is
        already waiting, and the pump writes it as the line plays.
        """
        if not pcm:
            return True
        if not self._start.audio_open:
            if not self.dropped_bytes:
                logger.warning("agent audio before the callee spoke is being dropped")
            self.dropped_bytes += len(pcm)
            return False
        item = self._items.setdefault(item_id, _Item())
        item.n_bytes += len(pcm)
        self.sent_bytes += len(pcm)
        if pcm.strip(b"\x00"):  # a vendor that streams digital silence (Vapi) is still quiet
            self._last_sent_at = self._monotonic()
        if not self._queue and not self._pumping() and self._lead_wait_s() <= 0:
            await self._write(pcm, item)
            return True
        self._queue.append((pcm, item_id))
        if not self._pumping():
            self._pump = asyncio.create_task(self._run_pump(), name="agent audio -> caller")
        return True

    def _pumping(self) -> bool:
        return self._pump is not None and not self._pump.done()

    def _lead_wait_s(self) -> float:
        """How long until the line is within ``PLAYBACK_LEAD_S`` of real time (<= 0: it is now)."""
        return self._playout_until - self._monotonic() - PLAYBACK_LEAD_S

    async def _write(self, pcm: bytes, item: _Item) -> None:
        self._playout_until = max(self._monotonic(), self._playout_until) + pcm_ms(len(pcm)) / 1000
        item.written_bytes += len(pcm)
        item.playout_until = self._playout_until
        await self._line.send(pcm)

    async def _run_pump(self) -> None:
        """Write the queue in order, each piece once the line is within the lead; once the line has
        closed, drop what is left."""
        try:
            while self._queue and not self._line.closed:
                if (wait := self._lead_wait_s()) > 0:
                    await self._sleep(wait)
                    continue  # an interruption or a hang-up may have come meanwhile
                pcm, item_id = self._queue.popleft()
                await self._write(pcm, self._items[item_id])
        except Exception:
            logger.exception("agent audio could not be written to the caller")
        self._drop_queue()

    def _drop_queue(self) -> dict[str, int]:
        dropped: dict[str, int] = {}
        while self._queue:
            pcm, item_id = self._queue.popleft()
            self._items[item_id].n_bytes -= len(pcm)
            self.sent_bytes -= len(pcm)
            dropped[item_id] = dropped.get(item_id, 0) + len(pcm)
        return {item_id: int(pcm_ms(n)) for item_id, n in dropped.items()}

    def played_ms(self, item_id: str) -> int:
        """How much of an item has played at the caller by now: what was written, less what the far
        end (playing at 1x, and silent while nothing is written) has not reached yet."""
        item = self._items.get(item_id)
        if item is None:
            return 0
        unplayed_ms = max(0.0, item.playout_until - self._monotonic()) * 1000
        return int(max(0.0, pcm_ms(item.written_bytes) - unplayed_ms))

    def heard_ms(self, item_id: str) -> int:
        """How much of an item the caller hears: everything written to the line, played or not (the
        ``voice_ws`` caller plays every byte it receives; nothing can clear its buffer)."""
        item = self._items.get(item_id)
        return int(pcm_ms(item.written_bytes)) if item else 0

    def sent_ms(self, item_id: str) -> int:
        """How much of an item was accepted: written, or still queued (back to what was written once
        an interruption drops the queue)."""
        item = self._items.get(item_id)
        return int(pcm_ms(item.n_bytes)) if item else 0

    def interrupted(self, *, drop_unsent: bool = False) -> dict[str, int]:
        """The vendor's interruption signal (the caller talked over the agent).

        The agent's memory must hold exactly what the caller heard. By default nothing is dropped:
        a vendor that keeps in its own context everything it sent us, and cannot be told what the
        caller heard, has all of it heard. ``drop_unsent=True`` drops the audio still queued
        (accepted, not yet written) and returns how many ms of it each item lost; only for a
        provider that then makes the vendor's context match ``heard_ms`` (OpenAI Realtime's
        truncate, the cascade's history). What was written still plays at the caller either way,
        so the playout clock runs on."""
        if not drop_unsent:
            return {}
        dropped = self._drop_queue()
        if dropped:
            logger.info("interruption: %d ms of queued agent audio dropped", sum(dropped.values()))
        return dropped

    async def drain(self) -> None:
        """Wait until the queue is written and everything written has played, never longer than
        ``MAX_DRAIN_S`` in all."""
        deadline = self._monotonic() + MAX_DRAIN_S
        while (pump := self._pump) is not None and not pump.done():
            if (left := deadline - self._monotonic()) <= 0:
                break
            await asyncio.wait({pump}, timeout=left)
        remaining = min(deadline, self._playout_until) - self._monotonic()
        if remaining > 0:
            await self._sleep(remaining)

    async def close(self) -> None:
        """The call is over: drop what is still queued and stop the pump."""
        self._drop_queue()
        if (pump := self._pump) is not None and not pump.done():
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    async def wait_until_quiet(self, quiet_s: float, max_wait_s: float, poll_s: float = 0.05) -> None:
        """Wait until no audible agent audio has arrived for ``quiet_s`` (giving up after ``max_wait_s``).

        For vendors that run the model on their side and may still be sending the last words
        when the agent's hang-up tool returns: the hang-up waits for the vendor to stop
        sending audio, then ``drain`` lets that audio play.
        """
        start = self._monotonic()
        while (now := self._monotonic()) - start < max_wait_s:
            if now - max(start, self._last_sent_at or start) >= quiet_s:
                return
            await asyncio.sleep(poll_s)


@dataclass
class CallRuntime:
    """Everything a provider needs for one call. Built by the server, one per connection."""

    line: CallerLine
    session: CallSession
    setup: SessionSetup
    start: CallStart
    inbound: InboundAudio
    outbound: OutboundAudio
    hung_up: bool = field(default=False, init=False)

    async def hang_up(self) -> None:
        """Let the last words play, apply any agent-requested grace, then close the caller's line."""
        if self.hung_up:
            return
        self.hung_up = True
        await self.outbound.drain()
        grace_s = getattr(self.session.state, "hang_up_grace_s", 0.0)
        if grace_s > 0:
            await asyncio.sleep(grace_s)
        await self.line.close()


class VoiceProvider(abc.ABC):
    """One voice vendor. Subclass it in ``vamos_voice/providers/<name>/`` and export it as ``PROVIDER``."""

    #: The vendor id: the folder name under providers/, the Docker ``VENDOR`` build arg and the extra.
    name: ClassVar[str]
    #: The bench ``trace_adapter`` this provider's traffic needs, or None when an SDK
    #: instrumentor already sees the model calls.
    trace_adapter: ClassVar[str | None]
    #: Environment variables the provider cannot start without (checked at startup).
    required_env: ClassVar[tuple[str, ...]]
    #: Can the vendor be asked for an opening turn (``agent_first``)? A vendor that cannot
    #: (Cartesia Managed Agents) refuses an agent that speaks first at startup.
    opens_calls: ClassVar[bool] = True

    @classmethod
    @abc.abstractmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """Build the provider from the process environment (keys, model, voice)."""

    @abc.abstractmethod
    async def run_call(self, call: CallRuntime) -> None:
        """Run one call to completion: return when the caller or the agent hangs up."""

    async def warm_up(self) -> None:  # noqa: B027 - optional hook, deliberately empty
        """Load anything slow (models, VAD) before /health reports ready."""

    async def close(self) -> None:  # noqa: B027 - optional hook, deliberately empty
        """Release what ``warm_up`` started (a sidecar process); runs when the server stops."""

    def router(self) -> APIRouter | None:
        """HTTP routes the vendor calls back on (a hosted orchestrator's tool webhook), or None.

        The server mounts them at ``/vendor/<name>`` (``vendor_route_path``). Every route must
        authenticate its caller itself (a per-call secret the provider handed the vendor) and
        must run tools only through the live call's ``CallSession.dispatch``.
        """
        return None


def vendor_route_path(provider_name: str, path: str) -> str:
    """The server path of one of a provider's routes: ``/vendor/<name><path>``."""
    return f"{VENDOR_ROUTE_PREFIX}/{provider_name}{path}"


def missing_env(provider: type[VoiceProvider], env: Mapping[str, str]) -> list[str]:
    return [key for key in provider.required_env if not env.get(key)]
