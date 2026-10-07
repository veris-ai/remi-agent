"""The one seam between an agent and a voice vendor.

A ``VoiceProvider`` turns one bench call into one vendor session. It receives
a ``CallRuntime`` holding everything vendor-neutral, already built and shared
by every provider:

- ``setup``: the rendered instructions, the tool specs (convert them with your
  ``ToolFormat``), and the turn-taking numbers every vendor maps to its own VAD;
- ``session``: send every tool call to ``session.dispatch`` and return its dict;
- ``start``: the ``CallStart`` (prime the connect reads, then the agent's
  start policy: hold the line for the callee, or open with a greeting);
- ``inbound`` / ``outbound``: caller audio in (held until priming finishes) and
  agent audio out (dropped until the callee has spoken; playout tracked for
  barge-in truncation and for hanging up after the last words).

A provider owns only what is genuinely vendor-specific: the wire protocol, the
tool-format conversion, how it forces the priming reads, and how it maps
barge-in. Everything a rule or a grader cares about lives above this line, and
no provider ever imports an agent: it sees only what the ``CallRuntime`` carries.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, Self

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ..audio import pcm_ms
from ..call_start import CallStart, CallStartPolicy
from ..session import CallSession
from ..tools import ToolSpec

logger = logging.getLogger(__name__)

#: Longest the caller's audio is held while priming runs; older audio is dropped first.
MAX_HELD_MS = 30_000
#: Longest the agent waits for its last words to play before closing the line.
MAX_DRAIN_S = 15.0


@dataclass(frozen=True)
class TurnTaking:
    """End-of-turn detection, the same for every vendor (mapped to each one's VAD)."""

    silence_ms: int = 800
    prefix_padding_ms: int = 300
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
    """The bench's ``voice_ws`` socket: PCM16 24 kHz mono binary frames both ways."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket  # for providers whose framework owns the transport (Pipecat)
        self.closed = False

    async def receive(self) -> bytes | None:
        """The next audio frame, or None once the caller hangs up."""
        while not self.closed:
            try:
                message = await self.websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                break
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                return message["bytes"]
            if message.get("text") is not None:
                logger.warning("caller sent a text frame on a binary audio channel; ignored")
        self.closed = True
        return None

    async def send(self, pcm: bytes) -> None:
        if self.closed or self.websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            await self.websocket.send_bytes(pcm)
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True

    async def close(self) -> None:
        if not self.closed and self.websocket.client_state == WebSocketState.CONNECTED:
            self.closed = True
            await self.websocket.close()


class InboundAudio:
    """Caller audio toward the vendor. Held while priming runs, then released in order."""

    def __init__(self, line: CallerLine, forward: Callable[[bytes], Awaitable[None]] | None = None) -> None:
        self._line = line
        self._forward = forward
        self._held = bytearray()
        self._open = False
        self._lock = asyncio.Lock()

    def bind(self, forward: Callable[[bytes], Awaitable[None]]) -> None:
        """Where caller audio goes (the provider's vendor send)."""
        self._forward = forward

    async def release(self) -> None:
        """Priming finished: send what was held, then pass audio straight through."""
        async with self._lock:
            self._open = True
            if self._held:
                held, self._held = bytes(self._held), bytearray()
                await self._send(held)

    async def run(self) -> None:
        """Pump until the caller hangs up."""
        cap = int(MAX_HELD_MS * 48)  # 24 kHz * 2 bytes per ms
        while (frame := await self._line.receive()) is not None:
            async with self._lock:
                if self._open:
                    await self._send(frame)
                else:
                    self._held.extend(frame)
                    if len(self._held) > cap:
                        del self._held[: len(self._held) - cap]

    async def _send(self, pcm: bytes) -> None:
        if self._forward is None:
            raise RuntimeError("InboundAudio has no destination: call bind() first")
        await self._forward(pcm)


@dataclass
class _Item:
    first_at: float
    n_bytes: int = 0


class OutboundAudio:
    """Agent audio toward the caller, through the call-start gate, with playout tracking."""

    def __init__(
        self, line: CallerLine, start: CallStart, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._line = line
        self._start = start
        self._monotonic = monotonic
        self._playout_until = 0.0
        self._items: dict[str, _Item] = {}
        self.dropped_bytes = 0
        self.sent_bytes = 0

    async def send(self, pcm: bytes, item_id: str = "") -> bool:
        """Forward agent audio; False (and dropped) while the line is held."""
        if not pcm:
            return True
        if not self._start.audio_open:
            if not self.dropped_bytes:
                logger.warning("agent audio before the callee spoke is being dropped")
            self.dropped_bytes += len(pcm)
            return False
        now = self._monotonic()
        item = self._items.setdefault(item_id, _Item(first_at=now))
        item.n_bytes += len(pcm)
        self._playout_until = max(now, self._playout_until) + pcm_ms(len(pcm)) / 1000
        self.sent_bytes += len(pcm)
        await self._line.send(pcm)
        return True

    def played_ms(self, item_id: str) -> int:
        """How much of an item the caller can have heard: no more than was sent, and no
        more than real time since its first byte (the far end plays at 1x)."""
        item = self._items.get(item_id)
        if item is None:
            return 0
        elapsed_ms = (self._monotonic() - item.first_at) * 1000
        return int(min(pcm_ms(item.n_bytes), elapsed_ms))

    def sent_ms(self, item_id: str) -> int:
        item = self._items.get(item_id)
        return int(pcm_ms(item.n_bytes)) if item else 0

    def interrupted(self) -> None:
        """The caller barged in: what was queued past this point will not be heard."""
        self._playout_until = self._monotonic()

    async def drain(self) -> None:
        """Wait until everything sent has had time to play."""
        remaining = min(MAX_DRAIN_S, self._playout_until - self._monotonic())
        if remaining > 0:
            await asyncio.sleep(remaining)


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
        """Let the last words play, then close the caller's line."""
        if self.hung_up:
            return
        self.hung_up = True
        await self.outbound.drain()
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

    @classmethod
    @abc.abstractmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """Build the provider from the process environment (keys, model, voice)."""

    @abc.abstractmethod
    async def run_call(self, call: CallRuntime) -> None:
        """Run one call to completion: return when the caller or the agent hangs up."""

    async def warm_up(self) -> None:  # noqa: B027 - optional hook, deliberately empty
        """Load anything slow (models, VAD) before /health reports ready."""


def missing_env(provider: type[VoiceProvider], env: Mapping[str, str]) -> list[str]:
    return [key for key in provider.required_env if not env.get(key)]
