from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator

import pytest

from vamos_agents.remi.agent import RemiAgent
from vamos_agents.remi.call import RemiCall
from vamos_agents.remi.context import CallContext
from vamos_agents.remi.db import Database
from vamos_agents.remi.payments import PayNearMe
from vamos_voice.context import CallClock
from vamos_voice.session import CallSession

from . import support
from .support import PostgresUnavailable, app_dsn, start_postgres, stop_postgres

PAYNEARME_BASE = "https://paynearme.twin.example"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    try:
        dsn, container = start_postgres()
    except PostgresUnavailable as err:
        pytest.skip(f"Postgres unavailable: {err}")
    yield dsn
    stop_postgres(container)


class ManualClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ticks() -> ManualClock:
    return ManualClock()


@pytest.fixture
def context() -> CallContext:
    return support.context("CARD-04")


def paynearme_client() -> PayNearMe:
    """Remi's PayNearMe client, wired to the test's twin (``support.PAYNEARME``) with the twin's credentials."""
    twin = support.PAYNEARME
    return PayNearMe(PAYNEARME_BASE, twin.site_identifier, twin.hmac_secret, transport=twin.transport())


@pytest.fixture
async def make_session(pg_dsn: str, ticks: ManualClock) -> AsyncIterator[Callable[..., CallSession]]:
    """Build the framework's CallSession around a RemiCall on a pilot task's hand-off (seed first with
    ``support.seed_world``): the exact dispatch path a real call takes. The twin's virtual clock is the
    call's time, as the World freezes it."""
    opened: list[Database] = []
    clients: list[PayNearMe] = []

    async def build(task: str = "CARD-04") -> CallSession:
        db = await Database.connect(app_dsn(pg_dsn))  # as the app login, which owns nothing
        opened.append(db)
        ctx = support.context(task)
        support.PAYNEARME.now = int(ctx.call_time.timestamp())
        payments = paynearme_client()
        clients.append(payments)
        clock = CallClock(ctx.call_time, monotonic=ticks)
        clock.start()
        call = RemiCall(ctx, clock, db, support.LEDGER, payments)
        return CallSession(call, RemiAgent.tools, RemiAgent.gates)

    yield build
    for db in opened:
        await db.close()
    for client in clients:
        await client.close()
