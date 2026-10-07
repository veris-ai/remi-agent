from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime

import pytest

from agents.remi.agent import CALLEE_FIRST, RemiAgent
from agents.remi.call import RemiCall
from agents.remi.context import CallContext
from agents.remi.db import Database
from vamos_voice.context import CallClock
from vamos_voice.session import CallSession

from .support import CALL_TIME, FakePayments, PostgresUnavailable, start_postgres, stop_postgres


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
    return CallContext(
        account_id="TAS-420101", phone_id="ph_420101_1", call_time=datetime.fromisoformat(CALL_TIME)
    )


@pytest.fixture
def payments() -> FakePayments:
    return FakePayments()


@pytest.fixture
async def make_session(
    pg_dsn: str, context: CallContext, payments: FakePayments, ticks: ManualClock
) -> AsyncIterator[Callable[..., CallSession]]:
    """Build the framework's CallSession around a RemiCall on the seeded world (seed first with
    ``support.seed_world``): the exact dispatch path a real call takes."""
    opened: list[Database] = []

    async def build(call_context: CallContext | None = None) -> CallSession:
        db = await Database.connect(pg_dsn)
        opened.append(db)
        ctx = call_context or context
        clock = CallClock(ctx.call_time, monotonic=ticks)
        clock.start()
        return CallSession(
            RemiCall(ctx, clock, db, payments), RemiAgent.tools, RemiAgent.gates, CALLEE_FIRST.connect_reads
        )

    yield build
    for db in opened:
        await db.close()
