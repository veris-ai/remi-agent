"""In-memory stand-ins for the caller's line, a vendor socket and the session.

They let a provider run a whole call offline, so tests can capture exactly
what it would send to its vendor and to the caller.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from remi_core.call_start import CALLEE_FIRST, CallStart
from remi_core.context import CallContext
from remi_core.prompt import render_instructions
from remi_core.providers.base import CallRuntime, InboundAudio, OutboundAudio, SessionSetup, TurnTaking
from remi_core.session import CONNECT_READS
from remi_core.tools import REGISTRY
from remi_core.world import Agency

from .support import CALL_TIME

AGENCY = Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")
CONTEXT = CallContext("TAS-420101", "ph_420101_1", datetime.fromisoformat(CALL_TIME))
HELLO = b"\x01\x00" * 4800  # 200 ms of PCM16 at 24 kHz: stands in for "Hello?"


def setup() -> SessionSetup:
    return SessionSetup(render_instructions(CONTEXT, AGENCY), REGISTRY.specs(), TurnTaking(), CALLEE_FIRST)


class FakeLine:
    """The caller's side of /voice: frames the test pushes in, audio the agent sent out."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self.websocket = None

    async def receive(self) -> bytes | None:
        frame = await self.incoming.get()
        if frame is None:
            self.closed = True
        return frame

    async def send(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


class ScriptedSession:
    """Records tool calls; the connect reads and end_call change state like the real session."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads: set[str] = set()
        self.ended = False

    @property
    def missing_reads(self) -> list[str]:
        return [r for r in CONNECT_READS if r not in self.reads]

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name in CONNECT_READS:
            self.reads.add(name)
        if name == "end_call":
            self.ended = True
        return {"ok": name}


def runtime(line: FakeLine | None = None, session: ScriptedSession | None = None) -> CallRuntime:
    line = line or FakeLine()
    start = CallStart(CALLEE_FIRST)
    return CallRuntime(
        line, session or ScriptedSession(), setup(), start, InboundAudio(line), OutboundAudio(line, start)
    )  # type: ignore[arg-type]


class FakeVendorSocket:
    """A vendor websocket: records what the provider sends, replays what the test scripts."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._sent_event = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))
        self._sent_event.set()

    async def recv(self) -> str:
        message = await self.inbox.get()
        if message is None:
            import websockets

            raise websockets.ConnectionClosedOK(None, None)
        return message

    def push(self, event: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(event))

    async def wait_for(self, predicate, within: float = 3.0) -> dict[str, Any]:
        """The first sent frame matching ``predicate``, waiting for it if needed."""

        async def find() -> dict[str, Any]:
            seen = 0
            while True:
                for frame in self.sent[seen:]:
                    if predicate(frame):
                        return frame
                seen = len(self.sent)
                self._sent_event.clear()
                await self._sent_event.wait()

        return await asyncio.wait_for(find(), within)

    def connector(self):
        @asynccontextmanager
        async def connect(url: str, headers: dict[str, str]):
            self.url, self.headers = url, headers
            yield self

        return connect
