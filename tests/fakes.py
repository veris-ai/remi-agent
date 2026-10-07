"""In-memory stand-ins for the caller's line, a vendor socket and the session, plus the agent cases.

They let a provider run a whole call offline, so tests can capture exactly
what it would send to its vendor and to the caller, for every agent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vamos_voice.agent import AgentSpec
from vamos_voice.call_start import CallStart, CallStartPolicy
from vamos_voice.context import AgentContext, CallClock
from vamos_voice.providers.base import CallRuntime, InboundAudio, OutboundAudio, SessionSetup, TurnTaking
from vamos_voice.server import session_setup

HELLO = b"\x01\x00" * 4800  # 200 ms of PCM16 at 24 kHz: stands in for "Hello?"


@dataclass(frozen=True)
class AgentCase:
    """One agent as the parity tests see it: what the server would hand every provider."""

    name: str
    build: Callable[[], AgentSpec]

    def agent(self) -> AgentSpec:
        return self.build()

    def setup(self) -> SessionSetup:
        agent = self.build()
        return session_setup(agent, CallClock(agent.call_time()), TurnTaking())

    @property
    def policy(self) -> CallStartPolicy:
        return self.build().call_start

    def expected_tools(self) -> dict[str, tuple[set[str], set[str]]]:
        return {
            s.name: (set(s.parameters["properties"]), set(s.parameters["required"]))
            for s in self.build().tools.specs()
        }


def _remi() -> AgentSpec:
    from vamos_agents.remi import RemiAgent
    from vamos_agents.remi.context import CallContext
    from vamos_agents.remi.world import Agency

    context = CallContext("TAS-420101", "ph_420101_1", datetime.fromisoformat("2027-02-10T09:00:00-05:00"))
    agent = RemiAgent(context, connect_db=_no_db, payments=None)  # type: ignore[arg-type]
    agent.agency = Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")
    return agent


async def _no_db() -> Any:
    raise AssertionError("parity tests never open the world")


def _fake_inbound() -> AgentSpec:
    from .fake_inbound import FakeInboundAgent

    return FakeInboundAgent.from_env({}, AgentContext.from_json(None))


REMI = AgentCase("remi", _remi)
FAKE_INBOUND = AgentCase("fake_inbound", _fake_inbound)
#: Every agent the parity matrix covers: each real pack, plus the test-only inbound agent.
AGENT_CASES = [REMI, FAKE_INBOUND]


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

    def __init__(self, connect_reads: tuple[str, ...]) -> None:
        self.connect_reads = connect_reads
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads: set[str] = set()
        self.ended = False

    @property
    def missing_reads(self) -> list[str]:
        return [r for r in self.connect_reads if r not in self.reads]

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name in self.connect_reads:
            self.reads.add(name)
        if name == "end_call":
            self.ended = True
        return {"ok": name}


def runtime(
    case: AgentCase, line: FakeLine | None = None, session: ScriptedSession | None = None
) -> CallRuntime:
    line = line or FakeLine()
    setup = case.setup()
    start = CallStart(setup.start_policy)
    return CallRuntime(
        line,  # type: ignore[arg-type]
        session or ScriptedSession(setup.start_policy.connect_reads),  # type: ignore[arg-type]
        setup,
        start,
        InboundAudio(line),  # type: ignore[arg-type]
        OutboundAudio(line, start),  # type: ignore[arg-type]
    )


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
