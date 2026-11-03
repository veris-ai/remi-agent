"""Pure rules: the call context, the clock, and the calendar and arithmetic Remi records with. No database.

Nothing here decides whether the agent may do something: the bench grades that. What is here is how Remi
writes what the agent did: business days for a confirmation letter, a plan's payments by World v2's
rounding, one disposition per call.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from vamos_agents.remi import policy
from vamos_agents.remi.context import CallContext
from vamos_voice.context import CallClock, ContextError

from .support import CONTEXT_JSON


def test_context_parses_the_bench_handoff() -> None:
    ctx = CallContext.from_json(CONTEXT_JSON)
    assert (ctx.account_id, ctx.phone_id) == ("TAS-261101", "ph_261101_1")
    assert ctx.call_date == date(2026, 11, 3)
    assert ctx.call_time == datetime.fromisoformat("2026-11-03T11:20:00-05:00")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"account_id": "TAS-261101", "phone_id": "ph_1"}),
        json.dumps({"account_id": "X-1", "phone_id": "ph_1", "call_time": "2026-11-03T11:20:00-05:00"}),
        json.dumps({"account_id": "TAS-261101", "phone_id": "ph_1", "call_time": "2026-11-03T11:20:00"}),
    ],
)
def test_context_rejects_anything_else(raw: str | None) -> None:
    with pytest.raises(ContextError):
        CallContext.from_json(raw)


def test_clock_is_call_time_plus_elapsed_never_wall_time() -> None:
    t = [50.0]
    clock = CallClock(datetime.fromisoformat("2026-11-03T20:59:30-05:00"), monotonic=lambda: t[0])
    assert clock.now() == clock.call_time  # before the call connects
    clock.start()
    t[0] += 45
    assert clock.now() == clock.call_time + timedelta(seconds=45)
    assert clock.now().year == 2026
    assert clock.elapsed == timedelta(seconds=45)


def test_federal_holidays() -> None:
    expected_2027 = {
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 10, 11),
        date(2027, 11, 11),
        date(2027, 11, 25),
        date(2027, 12, 24),
        date(2027, 12, 31),
    }
    assert policy.federal_holidays(2027) == expected_2027
    assert {date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25)} <= policy.federal_holidays(2026)


def test_a_confirmation_letter_is_due_in_5_business_days() -> None:
    """23 NYCRR 1.5: the World's day, then a week that crosses Veterans Day."""
    assert policy.confirmation_letter_due_by(date(2026, 11, 3)) == date(2026, 11, 10)
    assert policy.confirmation_letter_due_by(date(2026, 11, 9)) == date(2026, 11, 17)
    assert policy.confirmation_letter_due_by(date(2027, 2, 10)) == date(2027, 2, 18)  # Presidents' Day


def test_plan_payments_are_equal_rounded_down_and_the_last_takes_the_remainder() -> None:
    """World v2 (WORLD.md, "Creditor policies"): $576.00 in 7 is 6 x $82.28 + $82.32, never Fineract's half-even."""
    assert policy.plan_instalment_cents(57_600, 7) == [8_228] * 6 + [8_232]
    assert policy.plan_instalment_cents(94_500, 10) == [9_450] * 10
    assert policy.plan_instalment_cents(240_000, 7) == [34_285] * 6 + [34_290]
    assert policy.plan_instalment_cents(100, 3) == [33, 33, 34]
    for total, months in ((57_600, 7), (41_275, 36), (1, 1)):
        assert sum(policy.plan_instalment_cents(total, months)) == total


def test_a_plan_falls_one_payment_in_each_calendar_month() -> None:
    schedule = policy.monthly_due_dates(date(2026, 11, 30), 4)
    assert schedule == [date(2026, 11, 30), date(2026, 12, 30), date(2027, 1, 30), date(2027, 2, 28)]
    assert policy.add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)


def test_amounts_are_said_in_dollars_and_read_from_them() -> None:
    assert policy.dollars(144_000) == "$1,440.00" and policy.dollars(5) == "$0.05"
    assert policy.cents("1440.00") == 144_000 and policy.cents("412.75") == 41_275


def test_disposition_precedence() -> None:
    assert policy.disposition({"hardship", "plan"}, transferred=False) == "plan"
    assert policy.disposition({"callback", "hardship"}, transferred=False) == "hardship"
    assert policy.disposition(set(), transferred=True) == "transferred"
    assert policy.disposition({"dispute", "original_creditor_request"}, transferred=True) == "dispute"
    assert policy.disposition(set(), transferred=False) == "no_commitment"
