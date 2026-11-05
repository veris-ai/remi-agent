"""Focused harness behavior that does not need the World database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest

from vamos_agents.remi.agent import RemiAgent
from vamos_agents.remi.call import RemiCall
from vamos_agents.remi.context import CallContext
from vamos_agents.remi.world import Phone
from vamos_voice.context import CallClock
from vamos_voice.session import CallSession


class Tick:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class Transaction:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert(self, table: str, row: dict[str, Any]) -> None:
        self.db.rows.setdefault(table, []).append(dict(row))

    async def update(self, table: str, key: str, changes: dict[str, Any]) -> None:
        if table == "contact_attempts":
            (row,) = [row for row in self.db.rows[table] if row["attempt_id"] == key]
            row.update(changes)
        else:
            self.db.updates.append((table, key, dict(changes)))


class Database:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.updates: list[tuple[str, str, dict[str, Any]]] = []

    async def phone(self, phone_id: str) -> Phone:
        return Phone(phone_id, "con_1", "+12125550123", "mobile", "consumer", True, None, "valid")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        yield Transaction(self)


def session(*, transfer_grace_s: float = 3.0) -> tuple[CallSession, Database, Tick]:
    tick = Tick()
    clock = CallClock(datetime.fromisoformat("2027-02-10T09:00:00-05:00"), monotonic=tick)
    clock.start()
    db = Database()
    context = CallContext("TAS-1", "ph_1", datetime.fromisoformat("2027-02-10T09:00:00-05:00"))
    call = RemiCall(context, clock, db, None, None, transfer_grace_s)  # type: ignore[arg-type]
    return CallSession(call, RemiAgent.tools, RemiAgent.gates), db, tick


async def test_log_contact_attempt_writes_now_and_replaces_its_one_row() -> None:
    call, db, tick = session()
    tick.value = 42
    assert await call.dispatch(
        "log_contact_attempt", {"answered_by": "consumer", "summary": "The consumer answered the call."}
    ) == {"logged": True}
    (first,) = db.rows["contact_attempts"]
    assert first["at"] == datetime.fromisoformat("2027-02-10T09:00:00-05:00")  # the call's own time
    assert (first["answered_by"], first["duration_s"]) == ("consumer", 42)
    attempt_id = first["attempt_id"]

    tick.value = 50
    await call.dispatch(
        "log_contact_attempt", {"answered_by": "no_answer", "summary": "Correction: nobody answered."}
    )
    await call.finalize()
    (replaced,) = db.rows["contact_attempts"]
    assert replaced["attempt_id"] == attempt_id
    assert (replaced["answered_by"], replaced["duration_s"]) == ("no_answer", 50)


@pytest.mark.parametrize(
    ("tool", "args", "disposition"),
    [
        ("log_dispute", {"words": "This is not mine."}, "dispute"),
        ("record_flag", {"kind": "cease"}, "cease"),
        (
            "create_arrangement",
            {"kind": "discount", "total": "900.00", "first_due_on": "2027-02-20"},
            "settled",
        ),
        (
            "create_arrangement",
            {"kind": "payment_plan", "total": "900.00", "months": 6, "first_due_on": "2027-03-01"},
            "plan",
        ),
        (
            "create_arrangement",
            {"kind": "full_balance", "total": "900.00", "first_due_on": "2027-02-20"},
            "ptp",
        ),
    ],
)
async def test_fallback_disposition_comes_from_recorded_tools(
    tool: str, args: dict[str, Any], disposition: str
) -> None:
    call, db, _ = session()
    assert "error" not in await call.dispatch(tool, args)
    assert await call.dispatch("end_call", {}) == {"ended": True}
    (attempt,) = db.rows["contact_attempts"]
    assert (attempt["answered_by"], attempt["disposition"]) == (None, disposition)


async def test_transfer_ends_with_the_configured_post_playout_grace() -> None:
    call, db, _ = session(transfer_grace_s=1.5)
    assert await call.dispatch("transfer_to_human", {"reason": "consumer_asked_for_person"}) == {
        "transferred": True,
        "reason": "consumer_asked_for_person",
    }
    assert call.ended and call.state.hang_up_grace_s == 1.5
    (fallback,) = db.rows["contact_attempts"]
    assert (fallback["answered_by"], fallback["disposition"]) == (None, "transferred")


async def test_transfer_updates_an_existing_log_without_moving_its_time() -> None:
    call, db, tick = session(transfer_grace_s=1.5)
    tick.value = 10
    await call.dispatch(
        "log_contact_attempt", {"answered_by": "consumer", "summary": "The consumer asked for a person."}
    )
    (logged,) = db.rows["contact_attempts"]
    logged_at = logged["at"]
    tick.value = 20
    await call.dispatch("transfer_to_human", {"reason": "consumer_asked_for_person"})
    (transferred,) = db.rows["contact_attempts"]
    assert transferred["at"] == logged_at
    assert transferred["disposition"] == "transferred"
    assert "Transferred to a person." in transferred["summary"]
