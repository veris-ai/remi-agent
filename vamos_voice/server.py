"""The one FastAPI app every agent x vendor image runs: ``/health`` and ``/voice`` on one port.

``build_app_from_env`` checks everything a call needs before the port opens:
which agent and vendor, the agent's context and config, the vendor's keys.
Anything missing fails the process at startup with a message naming it, so a
misconfigured candidate never reaches a call. ``/health`` stays 503 until the
agent's world clients are connected and the provider has warmed up.

A provider may contribute HTTP routes (``VoiceProvider.router``: a hosted
orchestrator's tool webhook); they are mounted at ``/vendor/<name>`` and
authenticate their caller themselves. ``/voice`` takes exactly one call per pod (the bench's contract): it builds
the call's ``CallSession``, runs the agent's screen pop (its connect-time
reads, before any vendor session opens: ``vamos_voice.screen_pop``), builds
the ``CallRuntime`` and hands it to the provider. Whatever happens, the
agent's ``finalize()`` runs, once: the moment the caller hangs up or the
socket drops (``CallSession.caller_gone``, before the provider's teardown),
otherwise when the provider returns, and at the latest when the server shuts
down (a pod being stopped mid-call), before the agent's world clients close.
``VAMOS_VOICE_FRAMING`` picks the socket's framing (``vamos_voice.framing``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from .agent import AgentSpec, ConfigError, missing_agent_env
from .call_start import CallStart
from .context import AGENT_CONTEXT_ENV, AgentContext, CallClock
from .framing import Framing, framing_from_env
from .providers import load_provider
from .providers.base import (
    CallerLine,
    CallRuntime,
    InboundAudio,
    OutboundAudio,
    SessionSetup,
    TurnTaking,
    VoiceProvider,
    missing_env,
    vendor_route_path,
)
from .registry import load_agent
from .screen_pop import ScreenPop, run_screen_pop, with_screen_pop
from .session import CallSession

logger = logging.getLogger(__name__)

BUSY_CLOSE_CODE = 1013  # try again later
AGENT_ENV = "VAMOS_AGENT"
VENDOR_ENV = "VAMOS_VENDOR"

__all__ = [
    "ConfigError",
    "build_app_from_env",
    "call_runtime",
    "create_app",
    "run_call",
    "serve_call",
    "session_setup",
]


def session_setup(
    agent: AgentSpec, clock: CallClock, turn_taking: TurnTaking, screen_pop: ScreenPop | None = None
) -> SessionSetup:
    """What every provider sends its vendor for this agent's call (the parity tests check this)."""
    return SessionSetup(
        instructions=with_screen_pop(agent.instructions(clock), screen_pop),
        tools=agent.tools.specs(),
        turn_taking=turn_taking,
        start_policy=agent.call_start,
    )


@dataclass
class _Pod:
    ready: bool = False
    busy: bool = False
    calls: set[CallSession] = field(default_factory=set)


def create_app(
    agent: AgentSpec,
    provider: VoiceProvider,
    turn_taking: TurnTaking | None = None,
    framing: Framing = Framing.BINARY,
) -> FastAPI:
    """The app for one candidate pod."""
    pod = _Pod()
    turns = turn_taking or TurnTaking()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await agent.connect()
        await provider.warm_up()
        pod.ready = True
        logger.info("ready: agent=%s vendor=%s framing=%s", agent.describe(), provider.name, framing)
        try:
            yield
        finally:
            try:
                for session in list(pod.calls):  # a pod stopped mid-call still leaves the call's record
                    await _finalize(agent, session)
                await provider.close()
            finally:
                await agent.close()

    app = FastAPI(title=f"vamos-agents: {agent.name}", lifespan=lifespan)
    if (router := provider.router()) is not None:
        app.include_router(router, prefix=vendor_route_path(provider.name, ""))

    @app.get("/health")
    async def health() -> JSONResponse:
        if not pod.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ok", "agent": agent.name, "vendor": provider.name})

    @app.websocket("/voice")
    async def voice(websocket: WebSocket) -> None:
        if not pod.ready or pod.busy:
            await websocket.close(code=BUSY_CLOSE_CODE)
            return
        pod.busy = True
        await websocket.accept()
        await run_call(agent, provider, turns, websocket, framing, pod.calls)

    return app


async def run_call(
    agent: AgentSpec,
    provider: VoiceProvider,
    turns: TurnTaking,
    websocket: WebSocket,
    framing: Framing = Framing.BINARY,
    calls: set[CallSession] | None = None,
) -> None:
    """One call on the ``/voice`` socket, in the candidate's framing (see ``serve_call``)."""
    await serve_call(agent, provider, turns, CallerLine(websocket, framing), calls)


async def serve_call(
    agent: AgentSpec,
    provider: VoiceProvider,
    turns: TurnTaking,
    line: CallerLine,
    calls: set[CallSession] | None = None,
) -> None:
    """One call, start to finish. However it ends, the agent finalizes (at once when the caller
    hangs up) and the line hangs up through ``CallerLine.close`` (in ``json`` framing:
    ``{"type": "end"}``, then close 1000). ``calls`` holds the live session while the call runs,
    so a server shutting down can finalize it."""
    clock = CallClock(agent.call_time())
    clock.start()
    session = CallSession(agent.open_call(clock), agent.tools, agent.gates)
    line.on_caller_gone = session.caller_gone
    if calls is not None:
        calls.add(session)
    try:
        await provider.run_call(await call_runtime(agent, session, clock, turns, line))
    except Exception:
        logger.exception("call failed in provider %s", provider.name)
    finally:
        await _finalize(agent, session)
        if calls is not None:
            calls.discard(session)
        await line.close()
        logger.info(
            "call over after %.1fs; tools: %s", clock.elapsed.total_seconds(), ", ".join(session.tool_log)
        )


async def _finalize(agent: AgentSpec, session: CallSession) -> None:
    try:
        await session.finalize()
    except Exception:
        logger.exception("agent %s could not finalize the call", agent.name)


async def call_runtime(
    agent: AgentSpec, session: CallSession, clock: CallClock, turns: TurnTaking, line: CallerLine
) -> CallRuntime:
    """Everything the provider gets for one call. The agent's screen pop runs here, first: before
    any vendor session exists, so before any caller audio reaches a vendor or agent audio the caller."""
    pop = await run_screen_pop(agent, session.state)
    start = CallStart(agent.call_start)
    return CallRuntime(
        line,
        session,
        session_setup(agent, clock, turns, pop),
        start,
        InboundAudio(line),
        OutboundAudio(line, start),
    )


def build_app_from_env(env: Mapping[str, str]) -> FastAPI:
    """Validate the environment and build the app; raises ConfigError naming what is missing."""
    missing = [key for key in (AGENT_ENV, VENDOR_ENV) if not env.get(key)]
    if missing:
        raise ConfigError(f"missing environment: {', '.join(missing)}")
    try:
        agent_cls = load_agent(env[AGENT_ENV])
        context = AgentContext.from_json(env.get(AGENT_CONTEXT_ENV))
        if absent := missing_agent_env(agent_cls, env):
            raise ConfigError(f"agent {agent_cls.name} needs {', '.join(absent)}")
        agent = agent_cls.from_env(env, context)
        provider_cls = load_provider(env[VENDOR_ENV])
        if absent := missing_env(provider_cls, env):
            raise ConfigError(f"{provider_cls.name} needs {', '.join(absent)}")
        if agent_cls.call_start.agent_opens and not provider_cls.opens_calls:
            raise ConfigError(
                f"{provider_cls.name} cannot open a call, and agent {agent_cls.name} speaks first"
            )
        provider = provider_cls.from_env(env)
    except (ValueError, LookupError) as err:  # ContextError, ConfigError, Unknown{Agent,Vendor}
        raise ConfigError(str(err)) from err
    return create_app(agent, provider, framing=framing_from_env(env))
