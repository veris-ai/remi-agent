"""Pure rules: the call context, the clock, and RULES.md's numbers. No database."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from remi_core import policy
from remi_core.context import CallClock, CallContext, ContextError
from remi_core.world import Account, ContactAttempt, Phone

from .support import ACCOUNT, CONTEXT_JSON, PHONE

ET = datetime.fromisoformat("2027-02-10T09:00:00-05:00").tzinfo


def test_context_parses_the_bench_handoff() -> None:
    ctx = CallContext.from_json(CONTEXT_JSON)
    assert (ctx.account_id, ctx.phone_id) == ("TAS-420101", "ph_420101_1")
    assert ctx.call_date == date(2027, 2, 10)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"account_id": "TAS-420101", "phone_id": "ph_1"}),
        json.dumps({"account_id": "X-1", "phone_id": "ph_1", "call_time": "2027-02-10T09:00:00-05:00"}),
        json.dumps({"account_id": "TAS-420101", "phone_id": "ph_1", "call_time": "2027-02-10T09:00:00"}),
    ],
)
def test_context_rejects_anything_else(raw: str | None) -> None:
    with pytest.raises(ContextError):
        CallContext.from_json(raw)


def test_clock_is_call_time_plus_elapsed_never_wall_time() -> None:
    t = [50.0]
    clock = CallClock(datetime.fromisoformat("2027-02-10T20:59:30-05:00"), monotonic=lambda: t[0])
    assert clock.now() == clock.call_time  # before the call connects
    clock.start()
    t[0] += 45
    assert clock.now() == clock.call_time + timedelta(seconds=45)
    assert clock.now().year == 2027
    assert clock.elapsed == timedelta(seconds=45)


def test_federal_holidays_match_the_world_year() -> None:
    expected = {
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
    assert policy.federal_holidays(2027) == expected


def test_confirmation_letter_skips_presidents_day() -> None:
    assert policy.confirmation_letter_due_by(date(2027, 2, 10)) == date(2027, 2, 18)


def test_calling_window() -> None:
    def at(hm: str) -> datetime:
        return datetime.fromisoformat(f"2027-02-10T{hm}:00-05:00")

    assert not policy.in_calling_window(at("07:59"))
    assert policy.in_calling_window(at("08:00"))
    assert policy.in_calling_window(at("20:59"))
    assert not policy.in_calling_window(at("21:00"))
    assert not policy.in_calling_window(at("21:10"))


def _account(**changes: object) -> Account:
    return Account(**{k: v for k, v in {**ACCOUNT, **changes}.items() if k in Account.__dataclass_fields__})


def _phone(**changes: object) -> Phone:
    return Phone(**{**PHONE, **changes})


def _attempt(at: str, **changes: object) -> ContactAttempt:
    base = {
        "attempt_id": "a",
        "at": datetime.fromisoformat(at),
        "medium": "call",
        "direction": "outbound",
        "answered_by": "voicemail",
        "consumer_responded": False,
        "disposition": None,
        "summary": None,
    }
    return ContactAttempt(**{**base, **changes})


NOW = datetime.fromisoformat("2027-02-10T09:00:00-05:00")


@pytest.mark.parametrize(
    ("account", "phone", "history", "reason"),
    [
        ({"status": "cease"}, {}, [], "account_status_cease"),
        ({}, {"source": "employer"}, [], "employer_phone"),
        ({}, {"consent_revoked_at": NOW - timedelta(days=3)}, [], "consent_revoked"),
        ({}, {}, [_attempt(f"2027-02-0{d}T10:00:00-05:00") for d in (5, 7, 9)], "cap_reached"),
        (
            {},
            {},
            [_attempt("2027-02-08T10:00:00-05:00", answered_by="consumer", consumer_responded=True)],
            "responded_this_week",
        ),
        (
            {"sol_expires_on": date(2026, 1, 1), "time_barred_notice_mailed_on": date(2027, 2, 1)},
            {},
            [],
            "time_barred_notice_not_mailed_14_days",
        ),
    ],
)
def test_should_not_call(account: dict, phone: dict, history: list, reason: str) -> None:
    assert reason in policy.should_not_call_reasons(_account(**account), _phone(**phone), history, NOW)


def test_letters_and_no_answer_do_not_count_toward_the_cap() -> None:
    history = [
        _attempt("2027-02-09T10:00:00-05:00", medium="letter"),
        _attempt("2027-02-09T11:00:00-05:00", answered_by="no_answer"),
        _attempt("2027-02-09T12:00:00-05:00"),
        _attempt("2027-02-02T12:00:00-05:00"),
    ]
    assert policy.counted_attempts(history, NOW) == 1


def test_clean_account_may_be_called() -> None:
    assert policy.should_not_call_reasons(_account(), _phone(), [], NOW) == ()
    assert policy.should_not_call_reasons(_account(), _phone(), [], NOW.replace(hour=21, minute=10)) == (
        "outside_calling_window",
    )


def test_settlement_floors_round_up_and_add_ten_points_for_instalments() -> None:
    account = _account(current_balance_cents=200001, settlement_floor_pct=70)
    assert policy.settlement_floor_cents(account, 1) == 140001
    assert policy.settlement_floor_cents(account, 3) == 160001
    assert policy.settlement_refusal(account, 140000, 1).code == "below_floor"
    assert policy.settlement_refusal(account, 140001, 1) is None
    assert policy.settlement_refusal(account, 170000, 4).code == "too_many_settlement_instalments"


def test_plan_rules() -> None:
    account = _account(current_balance_cents=120000, max_plan_months=12, min_instalment_cents=2500)
    today = date(2027, 2, 10)
    assert policy.plan_refusal(account, 12, date(2027, 3, 1), today, hardship=False) is None
    assert (
        policy.plan_refusal(account, 36, date(2027, 3, 1), today, hardship=False).code == "term_above_maximum"
    )
    assert policy.plan_refusal(account, 24, date(2027, 3, 1), today, hardship=True) is None
    assert (
        policy.plan_refusal(account, 6, date(2027, 3, 20), today, hardship=False).code
        == "first_payment_outside_30_days"
    )
    small = _account(current_balance_cents=10000, min_instalment_cents=2500)
    assert (
        policy.plan_refusal(small, 5, date(2027, 3, 1), today, hardship=False).code
        == "instalment_below_minimum"
    )


def test_promise_window_and_instalment_schedule() -> None:
    today = date(2027, 2, 10)
    assert policy.ptp_refusal(date(2027, 2, 24), today) is None
    assert policy.ptp_refusal(date(2027, 2, 25), today) is not None
    schedule = policy.monthly_instalments(120000, 3, date(2027, 1, 31))
    assert [s["due_on"] for s in schedule] == ["2027-01-31", "2027-02-28", "2027-03-31"]
    assert sum(s["amount_cents"] for s in schedule) == 120000


def test_disposition_precedence() -> None:
    assert policy.disposition({"hardship", "hardship_plan"}, transferred=False) == "hardship_plan"
    assert policy.disposition({"callback", "hardship"}, transferred=False) == "hardship"
    assert policy.disposition(set(), transferred=True) == "transferred"
    assert policy.disposition({"dispute", "original_creditor_request"}, transferred=True) == "dispute"
    assert policy.disposition(set(), transferred=False) == "no_commitment"
