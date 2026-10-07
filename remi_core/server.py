"""The one FastAPI app every vendor image runs: ``/health`` and ``/voice`` on one port.

``build_app_from_env`` checks everything a call needs before the port opens:
the call context, the database and Stripe settings, the vendor and its keys.
Anything missing fails the process at startup with a message naming it, so a
misconfigured candidate never reaches a call. ``/health`` stays 503 until the
database answers and the provider has warmed up.

``/voice`` takes exactly one call per pod (the bench's contract): it builds
the call's ``CallSession`` and ``CallRuntime`` and hands them to the provider.
Whatever happens, the call leaves its contact-attempt row (``finalize``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from .call_start import CALLEE_FIRST, CallStart, CallStartPolicy
from .context import AGENT_CONTEXT_ENV, CallClock, CallContext
from .db import Database
from .payments import PaymentGateway, StripeGateway
from .prompt import render_instructions
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
)
from .session import CallSession
from .tools import REGISTRY, ToolRegistry
from .world import Agency

logger = logging.getLogger(__name__)

DB_CONNECT_ATTEMPTS = 10
DB_CONNECT_DELAY_S = 3.0
BUSY_CLOSE_CODE = 1013  # try again later


class ConfigError(RuntimeError):
    """The environment cannot run a call; the message names what is missing."""


@dataclass
class Dependencies:
    """Everything the app needs, injected so tests can supply fakes."""

    context: CallContext
    provider: VoiceProvider
    connect_db: Callable[[], Awaitable[Database]]
    payments: PaymentGateway
    registry: ToolRegistry = field(default_factory=lambda: REGISTRY)
    turn_taking: TurnTaking = field(default_factory=TurnTaking)
    start_policy: CallStartPolicy = CALLEE_FIRST


async def _connect_with_retry(connect: Callable[[], Awaitable[Database]]) -> Database:
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            return await connect()
        except Exception as err:  # the twin may still be starting; anything else fails after retries
            if attempt == DB_CONNECT_ATTEMPTS:
                raise
            logger.warning("database not reachable (attempt %d): %s", attempt, err)
            await asyncio.sleep(DB_CONNECT_DELAY_S)
    raise AssertionError("unreachable")


@dataclass
class _Pod:
    db: Database | None = None
    agency: Agency | None = None
    busy: bool = False

    @property
    def ready(self) -> bool:
        return self.db is not None and self.agency is not None


def create_app(deps: Dependencies) -> FastAPI:
    """The app for one candidate pod."""
    pod = _Pod()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db = await _connect_with_retry(deps.connect_db)
        agency = await db.agency()
        await deps.provider.warm_up()
        pod.db, pod.agency = db, agency
        logger.info("remi ready: vendor=%s account=%s", deps.provider.name, deps.context.account_id)
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(title="Remi", lifespan=lifespan)

    @app.get("/health")
    async def health() -> JSONResponse:
        if not pod.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ok", "vendor": deps.provider.name})

    @app.websocket("/voice")
    async def voice(websocket: WebSocket) -> None:
        if pod.db is None or pod.agency is None or pod.busy:
            await websocket.close(code=BUSY_CLOSE_CODE)
            return
        pod.busy = True
        await websocket.accept()
        await run_call(deps, websocket, pod.db, pod.agency)

    return app


async def run_call(deps: Dependencies, websocket: WebSocket, db: Database, agency: Agency) -> None:
    """One call, start to finish."""
    clock = CallClock(deps.context.call_time)
    clock.start()
    session = CallSession(deps.context, clock, db, deps.payments, deps.registry)
    setup = SessionSetup(
        instructions=render_instructions(deps.context, agency),
        tools=deps.registry.specs(),
        turn_taking=deps.turn_taking,
        start_policy=deps.start_policy,
    )
    line = CallerLine(websocket)
    start = CallStart(deps.start_policy)
    runtime = CallRuntime(line, session, setup, start, InboundAudio(line), OutboundAudio(line, start))
    try:
        await deps.provider.run_call(runtime)
    except Exception:
        logger.exception("call failed in provider %s", deps.provider.name)
    finally:
        try:
            await session.finalize()
        except Exception:
            logger.exception("could not write the call's contact attempt")
        await line.close()
        logger.info(
            "call over after %.1fs; tools: %s", clock.elapsed.total_seconds(), ", ".join(session.tool_log)
        )


def build_app_from_env(env: Mapping[str, str]) -> FastAPI:
    """Validate the environment and build the app; raises ConfigError naming what is missing."""
    try:
        context = CallContext.from_json(env.get(AGENT_CONTEXT_ENV))
    except ValueError as err:
        raise ConfigError(str(err)) from err
    missing = [key for key in ("DATABASE_URL", "STRIPE_API_KEY", "REMI_VENDOR") if not env.get(key)]
    if missing:
        raise ConfigError(f"missing environment: {', '.join(missing)}")
    provider_cls = load_provider(env["REMI_VENDOR"])
    if absent := missing_env(provider_cls, env):
        raise ConfigError(f"{provider_cls.name} needs {', '.join(absent)}")
    try:
        provider = provider_cls.from_env(env)
    except ValueError as err:
        raise ConfigError(str(err)) from err
    dsn = env["DATABASE_URL"]
    return create_app(
        Dependencies(
            context=context,
            provider=provider,
            connect_db=lambda: Database.connect(dsn),
            payments=StripeGateway(env["STRIPE_API_KEY"], env.get("STRIPE_API_BASE")),
        )
    )
