"""``RemiAgent``: Remi as an ``AgentSpec`` (outbound debt collection, the callee speaks first).

Remi works for Tallmere Account Services, LLC and places outbound calls to
consumers in New York City for the VAmoS Debt Collection benchmark. Its world
is one Postgres collections database (``DATABASE_URL``), opened in
``connect()``; payments go through the gateway functions inside it
(``payments.PaymentGateway``), so there is no second world client. When the
call connects, Remi's screen pop (``screen_pop``) reads the account, the
dialed phone and the contact history before any vendor session opens.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Self

from vamos_voice.agent import AgentSpec
from vamos_voice.call_start import callee_first
from vamos_voice.context import AgentContext, CallClock
from vamos_voice.screen_pop import ScreenPop

from .call import RemiCall
from .context import CallContext
from .db import Database
from .gates import GATES
from .prompt import render_instructions
from .screen_pop import screen_pop
from .tools import REGISTRY
from .world import Agency

logger = logging.getLogger(__name__)

DB_CONNECT_ATTEMPTS = 10
DB_CONNECT_DELAY_S = 3.0

#: Outbound: whoever answers speaks first.
CALLEE_FIRST = callee_first()


async def connect_with_retry(connect: Callable[[], Awaitable[Database]]) -> Database:
    """The collections twin may still be starting: retry, then fail loudly."""
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            return await connect()
        except Exception as err:  # the twin may still be starting; anything else fails after retries
            if attempt == DB_CONNECT_ATTEMPTS:
                raise
            logger.warning("database not reachable (attempt %d): %s", attempt, err)
            await asyncio.sleep(DB_CONNECT_DELAY_S)
    raise AssertionError("unreachable")


class RemiAgent(AgentSpec):
    """The debt-collection agent: callee first, the RULES.md opening as gates, one Postgres world."""

    name = "remi"
    call_start = CALLEE_FIRST
    tools = REGISTRY
    gates = GATES
    required_env = ("DATABASE_URL",)

    def __init__(self, context: CallContext, connect_db: Callable[[], Awaitable[Database]]) -> None:
        self.context = context
        self._connect_db = connect_db
        self.db: Database | None = None
        self.agency: Agency | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str], context: AgentContext) -> Self:
        call_context = CallContext.parse(context)
        dsn = env["DATABASE_URL"]
        return cls(call_context, connect_db=lambda: Database.connect(dsn))

    def call_time(self) -> datetime:
        return self.context.call_time

    async def connect(self) -> None:
        self.db = await connect_with_retry(self._connect_db)
        self.agency = await self.db.agency()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    def instructions(self, clock: CallClock) -> str:
        if self.agency is None:
            raise RuntimeError("RemiAgent.connect() must run before a call")
        return render_instructions(self.context, self.agency)

    def open_call(self, clock: CallClock) -> RemiCall:
        if self.db is None:
            raise RuntimeError("RemiAgent.connect() must run before a call")
        return RemiCall(self.context, clock, self.db)

    async def screen_pop(self, call: RemiCall) -> ScreenPop:
        return await screen_pop(call)

    def describe(self) -> str:
        return f"remi account={self.context.account_id}"
