"""In-memory stand-ins for the caller's line, a vendor socket and the session, plus the agent cases.

They let a provider run a whole call offline, so tests can capture exactly
what it would send to its vendor and to the caller, for every agent. An
optional shared ``events`` list records, in order, the reads a screen pop made
(``FakeRemiDb``), every frame sent to a vendor and every byte of agent audio,
so a test can check what happened first.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from vamos_voice.agent import AgentSpec
from vamos_voice.call_start import CallStart, CallStartPolicy
from vamos_voice.context import AgentContext, CallClock
from vamos_voice.framing import Framing
from vamos_voice.providers.base import CallRuntime, InboundAudio, OutboundAudio, SessionSetup, TurnTaking
from vamos_voice.server import call_runtime, session_setup
from vamos_voice.session import CallSession

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


REMI_CALL_TIME = datetime.fromisoformat("2027-02-10T09:00:00-05:00")
#: The verification secrets FakeRemiDb's consumer holds; none may reach the instructions.
REMI_SECRETS = ("1961-07-14", "1961", "11238", "4417", "Halsey St")


def _remi(db: Any = None) -> AgentSpec:
    from vamos_agents.remi import RemiAgent
    from vamos_agents.remi.context import CallContext
    from vamos_agents.remi.world import Agency

    context = CallContext("TAS-420101", "ph_420101_1", REMI_CALL_TIME)
    agent = RemiAgent(context, connect_db=_no_db)
    agent.agency = Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")
    agent.db = db
    return agent


async def _no_db() -> Any:
    raise AssertionError("parity tests never open the world")


class FakeRemiDb:
    """Remi's reads over an in-memory account that may be called (no Postgres): enough for the screen
    pop. Each read is recorded in ``events`` as ``sql:<table>``, as the World's SQL log would show it."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []

    async def agency(self) -> Any:
        from vamos_agents.remi.world import Agency

        self.events.append("sql:agency")
        return Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")

    async def account(self, account_id: str) -> Any:
        from vamos_agents.remi.world import Account

        self.events.append("sql:accounts")
        return Account(
            account_id, "con_1", "cred_1", "9921", date(2025, 3, 1), date(2024, 11, 1), 120_000, 0, 0, 0,
            120_000, None, date(2030, 11, 1), date(2025, 3, 5), None, 60, 12, 2_500, "active",
        )  # fmt: skip

    async def consumer(self, consumer_id: str) -> Any:
        from vamos_agents.remi.world import Consumer

        self.events.append("sql:consumers")
        return Consumer(
            consumer_id, "Marcus", "Sokolov", date(1961, 7, 14), "4417", "88 Halsey St", "Brooklyn",
            "11238", None, None, None, None,
        )  # fmt: skip

    async def creditor(self, creditor_id: str) -> Any:
        from vamos_agents.remi.world import Creditor

        self.events.append("sql:creditors")
        return Creditor(creditor_id, "Ashcombe Bank", "bank", None, False)

    async def phone(self, phone_id: str) -> Any:
        from vamos_agents.remi.world import Phone

        self.events.append("sql:phones")
        return Phone(phone_id, "con_1", "+17185550123", "mobile", "consumer_provided", True, None, "valid")

    async def contact_history(self, account_id: str) -> list[Any]:
        self.events.append("sql:contact_attempts")
        return []


def _fake_inbound() -> AgentSpec:
    from .fake_inbound import FakeInboundAgent

    return FakeInboundAgent.from_env({}, AgentContext.from_json(None))


REMI = AgentCase("remi", _remi)
FAKE_INBOUND = AgentCase("fake_inbound", _fake_inbound)
#: Every agent the parity matrix covers: each real pack, plus the test-only inbound agent.
AGENT_CASES = [REMI, FAKE_INBOUND]


class FakeLine:
    """The caller's side of /voice: frames the test pushes in, audio the agent sent out."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False
        self.websocket = None
        self.framing = Framing.BINARY
        self.events = events

    async def receive(self) -> bytes | None:
        frame = await self.incoming.get()
        if frame is None:
            self.caller_gone()
        return frame

    def caller_gone(self) -> None:
        self.closed = True

    async def send(self, pcm: bytes) -> None:
        self.sent.append(pcm)
        if self.events is not None:
            self.events.append("agent audio")

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


class ScriptedSession:
    """Records tool calls; end_call ends the call like the real session."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ended = False

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "end_call":
            self.ended = True
        return {"ok": name}


class RecordingSession(CallSession):
    """A real ``CallSession`` (real gates) that also records each call and its result."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
        result = await super().dispatch(name, raw_args)
        self.results.append((name, result))
        return result


def remi_gated_session() -> RecordingSession:
    """Remi's real per-call state and gates, with no world behind them (only gate refusals can run)."""
    from vamos_agents.remi.call import RemiCall

    agent = REMI.agent()
    state = RemiCall(agent.context, CallClock(agent.call_time()), None, None)  # type: ignore[arg-type]
    return RecordingSession(state, agent.tools, agent.gates)


def runtime(
    case: AgentCase, line: FakeLine | None = None, session: ScriptedSession | CallSession | None = None
) -> CallRuntime:
    """A call with no screen pop (the agent's bare instructions), driven by a scripted session."""
    line = line or FakeLine()
    setup = case.setup()
    start = CallStart(setup.start_policy)
    return CallRuntime(
        line,  # type: ignore[arg-type]
        session or ScriptedSession(),  # type: ignore[arg-type]
        setup,
        start,
        InboundAudio(line),  # type: ignore[arg-type]
        OutboundAudio(line, start),  # type: ignore[arg-type]
    )


async def remi_screen_pop_runtime(events: list[str], line: FakeLine) -> CallRuntime:
    """A Remi call built the way the server builds it (``server.call_runtime``): its real screen pop
    over ``FakeRemiDb``, its real state and gates (a ``RecordingSession``)."""
    agent = _remi(FakeRemiDb(events))
    clock = CallClock(agent.call_time())
    clock.start()
    session = RecordingSession(agent.open_call(clock), agent.tools, agent.gates)
    return await call_runtime(agent, session, clock, TurnTaking(), line)  # type: ignore[arg-type]


class FakeVendorSocket:
    """A vendor websocket: records what the provider sends, replays what the test scripts."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._sent_event = asyncio.Event()
        self.events = events

    async def send(self, message: str | bytes) -> None:
        # Binary frames (audio on mixed sockets: Deepgram, Fluxions) are recorded as {"binary": data}.
        frame = {"binary": message} if isinstance(message, bytes) else json.loads(message)
        self.sent.append(frame)
        if self.events is not None:
            self.events.append(f"vendor:{frame.get('type') or next(iter(frame))}")
        self._sent_event.set()

    async def recv(self) -> str | bytes:
        message = await self.inbox.get()
        if message is None:
            import websockets

            raise websockets.ConnectionClosedOK(None, None)
        return message

    def push(self, event: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(event))

    def push_binary(self, data: bytes) -> None:
        self.inbox.put_nowait(data)

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
