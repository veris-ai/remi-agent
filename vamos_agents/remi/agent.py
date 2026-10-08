"""``RemiAgent``: Remi as an ``AgentSpec`` (outbound debt collection, the callee speaks first).

Remi works for Tallmere Account Services, LLC and places outbound calls to
consumers in New York City for the VAmoS Debt Collection benchmark. Its world
is two clients, both opened in ``connect()``: the Postgres collections database
(``DATABASE_URL``), where the workflow lives and payments are charged through
the gateway functions inside it (``payments.PaymentGateway``), and the
Fineract ledger (``FINERACT_API_BASE`` and its credentials), where each
account's money lives (``ledger.FineractLedger``). When the call connects,
Remi's screen pop (``screen_pop``) reads the account, the dialed phone and the
contact history from Postgres before any vendor session opens; the ledger is
first read once the debt is (``get_debt_details``).
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
from .ledger import FineractLedger, Ledger
from .prompt import render_instructions
from .screen_pop import screen_pop
from .tools import REGISTRY
from .world import Agency

logger = logging.getLogger(__name__)

DB_CONNECT_ATTEMPTS = 10
DB_CONNECT_DELAY_S = 3.0
#: The Fineract credentials and tenant, beside the twin's address the bench injects.
LEDGER_ENV = ("FINERACT_API_BASE", "FINERACT_USER", "FINERACT_PASSWORD", "FINERACT_TENANT")

#: Outbound: whoever answers speaks first.
CALLEE_FIRST = callee_first()


async def connect_with_retry[T](what: str, connect: Callable[[], Awaitable[T]]) -> T:
    """A twin may still be starting: retry, then fail loudly."""
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            return await connect()
        except Exception as err:  # the twin may still be starting; anything else fails after retries
            if attempt == DB_CONNECT_ATTEMPTS:
                raise
            logger.warning("%s not reachable (attempt %d): %s", what, attempt, err)
            await asyncio.sleep(DB_CONNECT_DELAY_S)
    raise AssertionError("unreachable")


async def open_ledger(env: Mapping[str, str]) -> FineractLedger:
    """The Fineract ledger, connected (its payment types resolved); closed again if that fails."""
    ledger = FineractLedger(
        env["FINERACT_API_BASE"], env["FINERACT_USER"], env["FINERACT_PASSWORD"], env["FINERACT_TENANT"]
    )
    try:
        await ledger.connect()
    except BaseException:
        await ledger.close()
        raise
    return ledger


class RemiAgent(AgentSpec):
    """The debt-collection agent: callee first, the RULES.md opening as gates, Postgres and the ledger."""

    name = "remi"
    call_start = CALLEE_FIRST
    tools = REGISTRY
    gates = GATES
    required_env = ("DATABASE_URL", *LEDGER_ENV)

    def __init__(
        self,
        context: CallContext,
        connect_db: Callable[[], Awaitable[Database]],
        connect_ledger: Callable[[], Awaitable[Ledger]],
    ) -> None:
        self.context = context
        self._connect_db = connect_db
        self._connect_ledger = connect_ledger
        self.db: Database | None = None
        self.ledger: Ledger | None = None
        self.agency: Agency | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str], context: AgentContext) -> Self:
        call_context = CallContext.parse(context)
        dsn = env["DATABASE_URL"]
        ledger_env = {key: env[key] for key in LEDGER_ENV}
        return cls(
            call_context,
            connect_db=lambda: Database.connect(dsn),
            connect_ledger=lambda: open_ledger(ledger_env),
        )

    def call_time(self) -> datetime:
        return self.context.call_time

    async def connect(self) -> None:
        self.db = await connect_with_retry("database", self._connect_db)
        self.agency = await self.db.agency()
        self.ledger = await connect_with_retry("ledger", self._connect_ledger)
        today = self.context.call_time.date()
        if self.ledger.business_date != today:
            # Fineract refuses a posting after its business date, which the World holds at the call's day.
            logger.error(
                "the ledger's business date is %s, the call's is %s", self.ledger.business_date, today
            )

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
        if self.ledger is not None:
            await self.ledger.close()

    def instructions(self, clock: CallClock) -> str:
        if self.agency is None:
            raise RuntimeError("RemiAgent.connect() must run before a call")
        return render_instructions(self.context, self.agency)

    def open_call(self, clock: CallClock) -> RemiCall:
        if self.db is None or self.ledger is None:
            raise RuntimeError("RemiAgent.connect() must run before a call")
        return RemiCall(self.context, clock, self.db, self.ledger)

    async def screen_pop(self, call: RemiCall) -> ScreenPop:
        return await screen_pop(call)

    def describe(self) -> str:
        return f"remi account={self.context.account_id}"
