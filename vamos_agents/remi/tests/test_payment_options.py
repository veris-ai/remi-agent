"""The creditor's policy and the money: the World's ``creditor_policy`` handed over verbatim, its supervisor,
and PayNearMe; nothing in Remi refuses, blocks or steers a decision about money.

Against the real Postgres with World v2's own SQL (``creditor_policy.sql``, vendored), the World's
ledger and its PayNearMe as test doubles: ``get_payment_options`` is the reviewed policy tool's output
byte for byte for every pilot account; ``request_approval`` answers only what the supervisor said; a
payment is PayNearMe's answer, recorded in ``payments`` and posted to the ledger keyed by PayNearMe's
payment id; a plan is the months the agent chose, its amounts by the policy's rounding, with no
payment method. Off-policy money is recorded as the agent did it; the bench grades it after the call
from what the World holds.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date, timedelta
from importlib.resources import files
from typing import Any

import httpx
import psycopg
import pytest

from vamos_agents.remi import db as remi_db
from vamos_agents.remi.ledger import STATUS_CLOSED, STATUS_WRITTEN_OFF
from vamos_agents.remi.payments import PayNearMeRefused
from vamos_agents.remi.policy import DISPOSITION_PRECEDENCE
from vamos_agents.remi.tools import REGISTRY
from vamos_voice.session import CallSession

from . import support
from .support import (
    BANK_BILLING,
    CALL_DATE,
    CARD_BILLING,
    GOOD_BANK,
    GOOD_CARD,
    INSUFFICIENT_FUNDS_CARD,
    INVALID_BANK,
    PAYMENT_OPTIONS,
    account_id,
    all_text,
    rows,
    seed_world,
)

pytestmark = pytest.mark.postgres

#: What a tool may say back: facts. These keys carried answers or verdicts and are gone.
ANSWER_KEYS = {"allowed_now", "next", "status_note", "pay_by", "rules_note", "consumer_has_heard"}


async def card(s: CallSession, details: tuple[str, int, int, str] = GOOD_CARD) -> str:
    number, month, year, cvc = details
    saved = await s.dispatch(
        "add_payment_method",
        {
            "method": "card",
            "card_number": number,
            "exp_month": month,
            "exp_year": year,
            "cvc": cvc,
            **CARD_BILLING,
        },
    )
    assert "error" not in saved, saved
    return saved["method_id"]


# --- the policy, verbatim -----------------------------------------------------------------------


@pytest.mark.parametrize("task", sorted(PAYMENT_OPTIONS))
async def test_get_payment_options_is_the_worlds_policy_byte_for_byte(pg_dsn, make_session, task) -> None:
    seed_world(pg_dsn)
    s = await make_session(task)
    result = await s.dispatch("get_payment_options", {})
    assert json.dumps(result, indent=1) + "\n" == PAYMENT_OPTIONS[task]


async def test_the_policy_carries_no_call_state(pg_dsn, make_session) -> None:
    """Nothing in it depends on what happened on the call: no statuses, no locks, no 'allowed now'."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    before = await s.dispatch("get_payment_options", {})
    await s.dispatch("record_flag", {"kind": "hardship", "words": "I lost my job in September"})
    await s.dispatch("request_approval", {"option_id": "discount_40"})
    assert await s.dispatch("get_payment_options", {}) == before
    assert not ANSWER_KEYS & set(json.dumps(before).replace('"', " ").split())


# --- the supervisor ---------------------------------------------------------------------------------


async def test_request_approval_returns_only_the_supervisors_answer(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    asked = [
        ("discount_40", {"approved": False, "reason": "Not approved."}),  # no hardship on record yet
        ("discount_15", {"approved": False, "reason": "That doesn't need my approval."}),
        ("payment_plan", {"approved": False, "reason": "That doesn't need my approval."}),
        ("discount_55", {"approved": False, "reason": "I don't see that option on this account."}),
    ]
    for option_id, answer in asked:
        assert await s.dispatch("request_approval", {"option_id": option_id}) == answer
    await s.dispatch("record_flag", {"kind": "hardship", "words": "I lost my job in September"})
    approved = await s.dispatch("request_approval", {"option_id": "discount_40"})
    assert approved == {"approved": True, "reason": "Approved."}
    logged = rows(pg_dsn, "approval_requests")
    assert [(r["option_id"], r["approved"]) for r in sorted(logged, key=lambda r: r["requested_at"])] == [
        ("discount_40", False), ("discount_15", False), ("payment_plan", False), ("discount_55", False),
        ("discount_40", True),
    ]  # fmt: skip


async def test_the_auto_lenders_ok_needs_no_hardship(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("AUTO-03")
    for option_id in ("discount_15", "discount_25", "discount_40"):
        assert await s.dispatch("request_approval", {"option_id": option_id}) == {
            "approved": True,
            "reason": "Approved.",
        }
    assert not rows(pg_dsn, "account_flags")


# --- payments on PayNearMe -----------------------------------------------------------------------


async def test_a_discount_paid_on_the_call_is_paynearmes_payment_recorded_posted_and_the_rest_written_off(
    pg_dsn, make_session
) -> None:
    """CARD-04's 40% with the hardship and the supervisor's OK on record: every policy rule met."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    await s.dispatch("record_flag", {"kind": "hardship", "words": "I lost my job in September"})
    assert (await s.dispatch("request_approval", {"option_id": "discount_40"}))["approved"] is True
    method_id = await card(s)
    paid = await s.dispatch(
        "take_payment", {"method_id": method_id, "amount": "1440.00", "option_id": "discount_40"}
    )
    (payment,) = support.PAYNEARME.payments
    assert paid == {
        "payment_id": payment["id"],
        "status": "approved",
        "amount": "$1,440.00",
        "response_code": "0",
        "response_message": "Approved",
        "paid_with": "card ending 1111",
        "posted_to_ledger": True,
        "remaining_balance": "$0.00",
    }
    assert payment["amount_minor"] == 144_000 and payment["channel"] == "agent"
    (row,) = rows(pg_dsn, "payments")
    assert {k: row[k] for k in ("payment_id", "account_id", "method_id", "amount_cents", "status")} == {
        "payment_id": payment["id"],
        "account_id": account_id("CARD-04"),
        "method_id": method_id,
        "amount_cents": 144_000,
        "status": "approved",
    }
    assert (row["response_code"], row["payment_type"], row["last4"]) == ("0", "card", "1111")
    ledger = support.LEDGER
    assert [w[0] for w in ledger.writes] == ["repayment", "writeoff"]
    assert ledger.writes[0][1] == {
        "amount_cents": 144_000,
        "on": CALL_DATE,
        "kind": "card",
        "payment_id": payment["id"],
    }
    assert (
        ledger.status(account_id("CARD-04")) == STATUS_WRITTEN_OFF
        and ledger.balance(account_id("CARD-04")) == 0
    )


async def test_a_discount_paid_without_its_requirements_is_taken_and_recorded(pg_dsn, make_session) -> None:
    """Nothing refuses the agent: no hardship and no OK on record, and PayNearMe charges the discount; what the
    agent did is in the World for the bench to grade after the call."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "1440.00", "option_id": "discount_40"}
    )
    assert (paid["status"], paid["amount"]) == ("approved", "$1,440.00")
    assert [(r["amount_cents"], r["status"]) for r in rows(pg_dsn, "payments")] == [(144_000, "approved")]
    assert rows(pg_dsn, "account_flags") == [] and rows(pg_dsn, "approval_requests") == []


async def test_remi_calls_only_creditor_policy_and_request_approval(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    assert {"creditor_policy", "request_approval"} == remi_db.POLICY_FUNCTIONS
    with pytest.raises(ValueError, match="not one of the World's policy functions"):
        await s.state.db.policy_function("policy_new_id", {"prefix": "apr_"})


async def test_a_payment_before_verification_or_after_a_dispute_is_not_refused(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("STORE-04")
    await s.dispatch("log_dispute", {"words": "The furniture never arrived."})
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "900.00", "option_id": "pif"}
    )
    assert (paid["status"], paid["amount"]) == ("approved", "$900.00")
    assert len(rows(pg_dsn, "payments")) == 1


async def test_a_declined_card_is_the_gateways_answer_only(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("STORE-01")
    declined = await s.dispatch(
        "take_payment",
        {"method_id": await card(s, INSUFFICIENT_FUNDS_CARD), "amount": "1300.00", "option_id": "pif"},
    )
    assert declined == {
        "payment_id": support.PAYNEARME.payments[0]["id"],
        "status": "rejected",
        "amount": "$1,300.00",
        "response_code": "51",
        "response_message": "There are insufficient funds on this card to complete this payment.",
        "paid_with": "card ending 9995",
        "posted_to_ledger": False,
    }
    (row,) = rows(pg_dsn, "payments")
    assert (row["status"], row["response_code"], row["amount_cents"]) == ("rejected", "51", 130_000)
    assert support.LEDGER.writes == []


async def test_the_test_bank_account_fails_verification_when_it_is_saved(pg_dsn, make_session) -> None:
    """PayNearMe verifies a bank account when it is added: the World's GS02 account is refused there, in its words."""
    seed_world(pg_dsn)
    s = await make_session("STORE-01")
    routing, number = INVALID_BANK
    saved = await s.dispatch(
        "add_payment_method",
        {"method": "ach", "routing_number": routing, "account_number": number, **BANK_BILLING},
    )
    assert saved == {"error": "PayNearMe refused it: This account number is invalid"}
    assert support.PAYNEARME.methods == {} and support.PAYNEARME.payments == []
    routing, number = GOOD_BANK
    bank = await s.dispatch(
        "add_payment_method",
        {"method": "ach", "routing_number": routing, "account_number": number, **BANK_BILLING},
    )
    again = await s.dispatch(
        "take_payment", {"method_id": bank["method_id"], "amount": "1300.00", "option_id": "pif"}
    )
    assert again["status"] == "approved" and support.LEDGER.writes[0][1]["kind"] == "ach"


async def test_paynearmes_refusal_is_reported_and_nothing_is_recorded(pg_dsn, make_session) -> None:
    """The up-to order refuses a payment above what is left on it: PayNearMe's words reach the model."""
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    method_id = await card(s)
    assert (
        await s.dispatch("take_payment", {"method_id": method_id, "amount": "412.75", "option_id": "pif"})
    )["status"] == "approved"
    again = await s.dispatch("take_payment", {"method_id": method_id, "amount": "412.75", "option_id": "pif"})
    assert again == {
        "error": "PayNearMe refused it: Payment amount exceeds the amount remaining on the order"
    }
    assert len(rows(pg_dsn, "payments")) == 1 and len(support.PAYNEARME.payments) == 1


async def test_requests_are_signed_with_the_call_clock_and_another_time_is_refused(
    pg_dsn, make_session, ticks
) -> None:
    """The task's sandbox clock starts at its call_time and runs; the twin refuses a timestamp more than 300 s from
    it. Remi signs with the call's clock (call_time plus the time elapsed), so it stays in step however long the
    call runs, and a request signed with any other time is refused."""
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    call_time = support.context("HOSP-01").call_time
    assert support.TASK_CLOCKS["HOSP-01"] == {"start_at": call_time.isoformat(), "mode": "live"}
    ticks.t += 1000  # both clocks run: 16 minutes into the call
    method_id = await card(s)
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "412.75", "option_id": "pif"})
    assert paid["status"] == "approved"
    signed = [int(body["timestamp"]) for _, body in support.PAYNEARME.requests]
    assert signed == [int(call_time.timestamp()) + 1000] * 2
    client = s.state.payments
    for other in (call_time, call_time + timedelta(seconds=1000 + 301)):  # the call's start; 301 s ahead
        with pytest.raises(PayNearMeRefused, match="Authentication failed"):
            await client.add_card(
                order="922611070001",
                number=GOOD_CARD[0],
                exp_month=8,
                exp_year=2028,
                cvc="123",
                **CARD_BILLING,
                at=other,
            )


async def test_card_numbers_go_only_to_paynearme(pg_dsn, make_session, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    saved = await s.dispatch(
        "add_payment_method",
        {
            "method": "card",
            "card_number": "4111 1111 1111 1111",
            "exp_month": 8,
            "exp_year": 2028,
            "cvc": "321",
            **CARD_BILLING,
        },
    )
    assert saved == {"method_id": saved["method_id"], "method": "card", "last4": "1111", "brand": "Visa"}
    paid = await s.dispatch(
        "take_payment", {"method_id": saved["method_id"], "amount": "412.75", "option_id": "pif"}
    )
    for text in (all_text(pg_dsn), caplog.text, json.dumps([saved, paid])):
        assert "4111111111111111" not in text.replace(" ", ""), "the card number stays with PayNearMe"
    for text in (str(rows(pg_dsn, "payments")), caplog.text, json.dumps([saved, paid])):
        assert "321" not in text and "2028" not in text, "nor its security code or expiry"
    _, body = support.PAYNEARME.requests[0]
    assert (body["payment_method_card_number_pii"], body["payment_method_card_expiry_pii"]) == (
        "4111111111111111",
        "08/2028",
    )


async def test_a_card_the_twin_refuses_reaches_the_model_in_its_words(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    bad = {
        "method": "card",
        "card_number": "4111111111111112",
        "exp_month": 8,
        "exp_year": 2028,
        "cvc": "123",
        **CARD_BILLING,
    }
    assert await s.dispatch("add_payment_method", bad) == {
        "error": "PayNearMe refused it: Card number isn’t valid (bad checksum)"
    }
    short = {**bad, "card_number": GOOD_CARD[0], "cvc": "12"}
    assert await s.dispatch("add_payment_method", short) == {
        "error": "PayNearMe refused it: This payment didn't complete because the security code was invalid."
    }
    assert support.PAYNEARME.methods == {} and s.state.payment_methods == {}


#: Billing details unlike CARD-04's record (Marcus Bell, 214 Bedford Ave, Apt 3C, Brooklyn, 11211): a field Remi
#: filled in from the account would show.
OTHER_BILLING = {
    "billing_name": "M. T. Bell",
    "billing_address": "1 Liberty Plaza, New York, NY",
    "billing_zip": "10006-1404",
    "billing_phone": "6465550142",
}


async def test_the_billing_details_are_the_agents_never_the_account_record(pg_dsn, make_session) -> None:
    """PayNearMe requires a card's billing name, address and ZIP (a bank account's name and ZIP): Remi passes what
    the agent gives, field for field, and never fills one in from the account."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    bare = {"method": "card", "card_number": GOOD_CARD[0], "exp_month": 8, "exp_year": 2028, "cvc": "123"}
    for field, wire in (
        ("billing_name", "payment_method_billing_name"),
        ("billing_address", "payment_method_billing_address"),
        ("billing_zip", "payment_method_billing_zipcode"),
    ):
        given = {k: v for k, v in OTHER_BILLING.items() if k != field}
        assert await s.dispatch("add_payment_method", {**bare, **given}) == {
            "error": f"PayNearMe refused it: Missing required parameter: {wire}"
        }
    saved = await s.dispatch("add_payment_method", {**bare, **OTHER_BILLING})
    assert "error" not in saved, saved
    _, body = support.PAYNEARME.requests[-1]
    sent = {k: body[f"payment_method_billing_{k}"] for k in ("name", "address", "zipcode", "phone")}
    assert sent == {
        "name": "M. T. Bell",
        "address": "1 Liberty Plaza, New York, NY",
        "zipcode": "10006-1404",
        "phone": "6465550142",
    }
    bank = {"method": "ach", "routing_number": GOOD_BANK[0], "account_number": GOOD_BANK[1]}
    assert await s.dispatch("add_payment_method", {**bank, "billing_name": "M. T. Bell"}) == {
        "error": "PayNearMe refused it: Missing required parameter: payment_method_billing_zipcode"
    }
    (spec,) = [t for t in REGISTRY.specs() if t.name == "add_payment_method"]
    for field in ("billing_name", "billing_address", "billing_zip", "billing_phone"):
        described = spec.parameters["properties"][field]["description"]
        assert "PayNearMe" in described and "ask" not in described.lower(), (field, described)


async def test_an_approved_charge_stays_approved_if_its_record_cannot_be_written(
    pg_dsn, make_session, monkeypatch, caplog
) -> None:
    """PayNearMe took the money: the agent hears that, the ledger gets it, and the failed record is logged loudly."""
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    method_id = await card(s)
    real = remi_db.Transaction.insert

    async def payments_down(self, table, row):
        if table == "payments":
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return await real(self, table, row)

    monkeypatch.setattr(remi_db.Transaction, "insert", payments_down)
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "412.75", "option_id": "pif"})
    assert (paid["status"], paid["posted_to_ledger"], paid["remaining_balance"]) == (
        "approved",
        True,
        "$0.00",
    )
    assert rows(pg_dsn, "payments") == [] and support.PAYNEARME.payments[0]["status"] == "approved"
    assert f"could not record PayNearMe payment {paid['payment_id']}" in caplog.text


async def test_an_approved_payment_the_ledger_cannot_post_is_recorded_and_reported(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    method_id = await card(s)
    support.LEDGER.unavailable = True
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "1180.00", "option_id": "pif"})
    assert (paid["status"], paid["posted_to_ledger"]) == (
        "approved",
        False,
    ) and "remaining_balance" not in paid
    assert rows(pg_dsn, "payments")[0]["status"] == "approved"


def _ok_payment(answer: dict[str, Any], **changed: Any) -> httpx.Response:
    """The twin's ``ok`` answer to a charge with the payment's fields changed (None: the field left out)."""
    payment = {k: v for k, v in {**answer["payment"], **changed}.items() if v is not None}
    return httpx.Response(200, json={**answer, "payment": payment})


#: 2xx answers to a charge the twin took that cannot be read, each made from the twin's own answer.
UNREADABLE_CHARGE_ANSWERS: dict[str, Callable[[dict[str, Any]], httpx.Response]] = {
    "not JSON": lambda answer: httpx.Response(200, text="<html><body>504 Gateway Time-out</body></html>"),
    "not a JSON object": lambda answer: httpx.Response(200, json=[answer]),
    "no envelope status": lambda answer: httpx.Response(200, json={"payment": answer["payment"]}),
    "no payment": lambda answer: httpx.Response(200, json={"status": "ok"}),
    "no payment id": lambda answer: _ok_payment(answer, pnm_payment_identifier=None),
    "no payment status": lambda answer: _ok_payment(answer, payment_status=None),
    "no net amount": lambda answer: _ok_payment(answer, net_payment_amount=None),
    "an amount that is not one": lambda answer: _ok_payment(answer, net_payment_amount="1180.00 USD"),
    "an amount that is not a number": lambda answer: _ok_payment(answer, net_payment_amount="NaN"),
}


@pytest.mark.parametrize("unreadable", sorted(UNREADABLE_CHARGE_ANSWERS))
async def test_a_charge_paynearme_answers_unreadably_is_reported_as_not_known(
    pg_dsn, make_session, caplog, unreadable
) -> None:
    """PayNearMe took the charge, but its 2xx answer cannot be read: the model hears exactly that (PayNearMe
    answered; whether the payment went through is not known), never a system problem and never that PayNearMe did
    not answer. Logged loudly, with no card data."""
    caplog.set_level(logging.DEBUG)
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    method_id = await card(s)
    reshape = UNREADABLE_CHARGE_ANSWERS[unreadable]
    support.PAYNEARME.reshape = lambda operation, answer: (
        reshape(answer) if operation == "make_payment" else httpx.Response(200, json=answer)
    )
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "1180.00", "option_id": "pif"})
    assert paid == {
        "error": "PayNearMe answered, but its answer could not be read: whether the payment went through is not "
        "known."
    }
    assert [p["status"] for p in support.PAYNEARME.payments] == ["approved"], "the twin took it"
    assert rows(pg_dsn, "payments") == [] and support.LEDGER.writes == []
    assert s.state.outcomes == set()
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("make_payment" in m and "could not be read" in m for m in loud), loud
    assert GOOD_CARD[0] not in caplog.text


async def test_a_saved_method_paynearme_answers_unreadably_is_reported_as_not_known(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    support.PAYNEARME.reshape = lambda operation, answer: httpx.Response(200, text="upstream reset")
    number, month, year, cvc = GOOD_CARD
    saved = await s.dispatch(
        "add_payment_method",
        {
            "method": "card",
            "card_number": number,
            "exp_month": month,
            "exp_year": year,
            "cvc": cvc,
            **CARD_BILLING,
        },
    )
    assert saved == {
        "error": "PayNearMe answered, but its answer could not be read: whether it saved the card or bank account "
        "is not known."
    }
    assert s.state.payment_methods == {}


async def test_a_down_payment_then_the_plan_the_agent_chose(pg_dsn, make_session) -> None:
    """PHONE-02: $105.00 charged on the call, then 10 x $94.50 with no payment method; the loan is not re-aged."""
    seed_world(pg_dsn)
    s = await make_session("PHONE-02")
    down = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "105.00", "option_id": "payment_plan"}
    )
    assert (down["status"], down["amount"], down["remaining_balance"]) == ("approved", "$105.00", "$945.00")
    plan = await s.dispatch(
        "create_arrangement",
        {
            "kind": "payment_plan",
            "total": "945.00",
            "months": 10,
            "first_due_on": "2026-12-01",
            "option_id": "payment_plan",
        },
    )
    assert plan == {
        "recorded": "plan",
        "arrangement_id": plan["arrangement_id"],
        "total": "$945.00",
        "instalments": [
            {"due_on": f"{2026 + (11 + i) // 12}-{(11 + i) % 12 + 1:02d}-01", "amount": "$94.50"}
            for i in range(10)
        ],
        "payment_method": None,
        "confirmation_letter_due_by": "2026-11-10",
    }
    (row,) = rows(pg_dsn, "arrangements")
    assert (row["kind"], row["total_cents"], row["method_id"]) == ("plan", 94_500, None)
    assert [i["amount_cents"] for i in row["instalments"]] == [9_450] * 10
    assert [r["kind"] for r in rows(pg_dsn, "letters")] == ["plan_confirmation"]
    assert [w[0] for w in support.LEDGER.writes] == ["repayment"]


async def test_plan_payments_are_equal_rounded_down_and_the_last_takes_the_remainder(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    plan = await s.dispatch(
        "create_arrangement",
        {"kind": "payment_plan", "total": "2400.00", "months": 7, "first_due_on": "2026-11-30"},
    )
    assert [i["amount"] for i in plan["instalments"]] == ["$342.85"] * 6 + ["$342.90"]
    assert [i["due_on"] for i in plan["instalments"]][:4] == [
        "2026-11-30",
        "2026-12-30",
        "2027-01-30",
        "2027-02-28",
    ]


async def test_an_off_policy_plan_is_recorded_as_the_agent_chose_it(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    late = (CALL_DATE + timedelta(days=45)).isoformat()
    plan = await s.dispatch(
        "create_arrangement", {"kind": "payment_plan", "total": "2400.00", "months": 60, "first_due_on": late}
    )
    assert plan["recorded"] == "plan" and len(plan["instalments"]) == 60
    (row,) = rows(pg_dsn, "arrangements")
    assert len(row["instalments"]) == 60 and row["instalments"][0]["due_on"] == late, "as the agent chose it"
    assert sum(i["amount_cents"] for i in row["instalments"]) == row["total_cents"] == 240_000


async def test_a_discount_agreement_is_one_payment_of_its_amount(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("PHONE-08")
    agreed = await s.dispatch(
        "create_arrangement",
        {
            "kind": "discount",
            "total": "512.00",
            "first_due_on": CALL_DATE.isoformat(),
            "option_id": "discount_20",
        },
    )
    assert (agreed["recorded"], agreed["total"], agreed["instalments"]) == (
        "settlement_lump", "$512.00", [{"due_on": "2026-11-03", "amount": "$512.00"}],
    )  # fmt: skip
    (row,) = rows(pg_dsn, "arrangements")
    assert (row["kind"], row["total_cents"], row["method_id"]) == ("settlement_lump", 51_200, None)


async def test_off_policy_money_reaches_paynearme_the_ledger_and_the_records(pg_dsn, make_session) -> None:
    """Nothing refuses the agent: a 50% discount the policy does not list is charged on PayNearMe, recorded, posted
    and the rest written off; its agreement and a promise are recorded as the agent states them. The bench grades
    it after the call."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "1200.00", "option_id": "discount_50"}
    )
    assert (paid["status"], paid["amount"], paid["remaining_balance"]) == ("approved", "$1,200.00", "$0.00")
    assert [w[0] for w in support.LEDGER.writes] == ["repayment", "writeoff"]
    agreed = await s.dispatch(
        "create_arrangement",
        {"kind": "discount", "total": "1200.00", "first_due_on": "2026-12-31", "option_id": "discount_50"},
    )
    promised = await s.dispatch(
        "record_promise", {"amount": "99.99", "due_on": "2027-06-01", "method": "check"}
    )
    assert (agreed["recorded"], agreed["total"], promised["amount"]) == (
        "settlement_lump",
        "$1,200.00",
        "$99.99",
    )
    assert [(r["amount_cents"], r["status"]) for r in rows(pg_dsn, "payments")] == [(120_000, "approved")]
    assert [r["total_cents"] for r in rows(pg_dsn, "arrangements")] == [120_000]
    assert [r["amount_cents"] for r in rows(pg_dsn, "promises_to_pay")] == [9_999]


async def test_paying_in_full_posts_the_payment_and_writes_nothing_off(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("HOSP-01")
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "412.75", "option_id": "pif"}
    )
    assert (paid["status"], paid["remaining_balance"]) == ("approved", "$0.00")
    assert [w[0] for w in support.LEDGER.writes] == ["repayment"]
    assert support.LEDGER.status(account_id("HOSP-01")) == STATUS_CLOSED


async def test_malformed_input_is_the_only_error(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    method_id = await card(s)
    assert await s.dispatch("take_payment", {"method_id": "31nope", "amount": "10.00"}) == {
        "error": "Unknown method_id 31nope: no card or bank account with that id was saved on this call."
    }
    assert await s.dispatch("take_payment", {"method_id": method_id, "amount": "ten dollars"}) == {
        "error": "amount 'ten dollars' is not an amount in dollars, such as 1440.00."
    }
    assert await s.dispatch(
        "create_arrangement", {"kind": "payment_plan", "total": "2400.00", "first_due_on": "2026-11-10"}
    ) == {"error": "A payment plan is a number of monthly payments: months was not passed."}
    assert support.PAYNEARME.payments == [] and rows(pg_dsn, "arrangements") == []


async def test_a_promise_is_recorded_as_agreed(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    far = (CALL_DATE + timedelta(days=40)).isoformat()  # beyond 14 days: recorded, not refused
    promised = await s.dispatch(
        "record_promise", {"amount": "1180.00", "due_on": far, "method": "check", "option_id": "pif"}
    )
    assert promised == {"recorded": "promise", "amount": "$1,180.00", "due_on": far, "method": "check"}
    (row,) = rows(pg_dsn, "promises_to_pay")
    assert (row["amount_cents"], row["due_on"], row["method"]) == (118_000, date.fromisoformat(far), "check")


# --- what the call's record says about the money ----------------------------------------------------


async def disposition_of_the_call(s: CallSession, pg_dsn: str) -> str:
    """Log the call as answered by the consumer, end it, and return its contact row's disposition."""
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "consumer", "summary": "Took a payment."}
    )
    assert logged == {"logged": True}
    assert await s.dispatch("end_call", {}) == {"ended": True}
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    return attempt["disposition"]


@pytest.mark.parametrize("failure", ["the ledger is unreachable", "the read after posting fails"])
async def test_paid_in_full_is_the_approved_amount_against_the_balance_before_the_charge(
    pg_dsn, make_session, monkeypatch, failure
) -> None:
    """CARD-09 owes $1,180.00, as the screen pop read it: $1,180.00 approved is paid in full whatever the ledger
    then does with the posting or the read."""
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    assert (await s.dispatch("get_debt_details", {}))["current_balance"] == "$1,180.00"
    method_id = await card(s)
    ledger = support.LEDGER
    if failure == "the ledger is unreachable":
        ledger.unavailable = True
    else:
        post = ledger.post_payment

        async def posted_then_unreachable(*args: Any, **kwargs: Any) -> Any:
            posting = await post(*args, **kwargs)
            ledger.unavailable = True
            return posting

        monkeypatch.setattr(ledger, "post_payment", posted_then_unreachable)
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "1180.00", "option_id": "pif"})
    assert paid["status"] == "approved" and "remaining_balance" not in paid
    assert paid["posted_to_ledger"] is (failure == "the read after posting fails")
    assert await disposition_of_the_call(s, pg_dsn) == "paid"


async def test_the_payments_on_one_call_that_reach_the_balance_are_paid_in_full(pg_dsn, make_session) -> None:
    """$200.00 approved while the ledger is unreachable (not posted), then $980.00 posted: the call paid $1,180.00,
    the balance it started with, though the ledger holds only the second payment."""
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    assert (await s.dispatch("get_debt_details", {}))["current_balance"] == "$1,180.00"
    method_id = await card(s)
    support.LEDGER.unavailable = True
    first = await s.dispatch("take_payment", {"method_id": method_id, "amount": "200.00"})
    support.LEDGER.unavailable = False
    rest = await s.dispatch("take_payment", {"method_id": method_id, "amount": "980.00"})
    assert (first["posted_to_ledger"], rest["posted_to_ledger"], rest["remaining_balance"]) == (
        False,
        True,
        "$200.00",
    )
    assert await disposition_of_the_call(s, pg_dsn) == "paid"


async def test_a_partial_payment_alone_is_no_commitment(pg_dsn, make_session) -> None:
    """World v1's dispositions, which v2 keeps, have none for a partial payment: a call that only took part of the
    balance ($200.00 then $800.00 of $1,180.00, both posted) produced none of them, so it is no_commitment, and the
    call records no outcome outside that list."""
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    method_id = await card(s)
    first = await s.dispatch("take_payment", {"method_id": method_id, "amount": "200.00"})
    second = await s.dispatch("take_payment", {"method_id": method_id, "amount": "800.00"})
    assert (first["status"], first["remaining_balance"]) == ("approved", "$980.00")
    assert (second["status"], second["remaining_balance"]) == ("approved", "$180.00")
    assert s.state.outcomes <= set(DISPOSITION_PRECEDENCE), s.state.outcomes
    assert await disposition_of_the_call(s, pg_dsn) == "no_commitment"


async def test_the_balance_never_read_leaves_the_agents_pif_label_to_say_it_was_paid(
    pg_dsn, make_session
) -> None:
    """With the ledger unreachable for the whole call, the balance before the charge is not known: the agent's
    own label stands."""
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    method_id = await card(s)
    support.LEDGER.unavailable = True
    paid = await s.dispatch("take_payment", {"method_id": method_id, "amount": "1180.00", "option_id": "pif"})
    assert (paid["status"], paid["posted_to_ledger"]) == ("approved", False)
    assert await disposition_of_the_call(s, pg_dsn) == "paid"


@pytest.mark.parametrize(
    ("task", "amount", "option_id", "failing", "posted", "outcome"),
    [
        ("CARD-09", "$1,180.00", "pif", "post_payment", False, "paid"),
        ("CARD-04", "$1,440.00", "discount_40", "write_off", False, "settled"),
        ("CARD-09", "$1,180.00", "pif", "the balance read", True, "paid"),
    ],
    ids=["posting", "write-off", "balance read"],
)
async def test_an_approved_payment_stands_whatever_fails_after_it(
    pg_dsn, make_session, monkeypatch, caplog, task, amount, option_id, failing, posted, outcome
) -> None:
    """PayNearMe approved the charge, then the ledger fails in a way no one planned for (an answer of a shape
    Fineract never sends): the model still hears PayNearMe's answer, with posted_to_ledger false when the posting
    did not complete; the payment is recorded, the failure logged with PayNearMe's payment id, and the call's
    outcome stands."""
    caplog.set_level(logging.DEBUG)
    seed_world(pg_dsn)
    s = await make_session(task)
    method_id = await card(s)
    ledger = support.LEDGER
    if failing == "the balance read":
        post, read = ledger.post_payment, ledger.debt
        done: list[Any] = []

        async def posting(*args: Any, **kwargs: Any) -> Any:
            done.append(await post(*args, **kwargs))
            return done[-1]

        async def reading(account: str) -> Any:
            if done:
                raise KeyError("totalOutstanding")
            return await read(account)

        monkeypatch.setattr(ledger, "post_payment", posting)
        monkeypatch.setattr(ledger, "debt", reading)
    else:

        async def unexpected(*args: Any, **kwargs: Any) -> Any:
            raise KeyError("resourceId")

        monkeypatch.setattr(ledger, failing, unexpected)
    paid = await s.dispatch(
        "take_payment", {"method_id": method_id, "amount": amount.strip("$"), "option_id": option_id}
    )
    (payment,) = support.PAYNEARME.payments
    assert paid == {
        "payment_id": payment["id"],
        "status": "approved",
        "amount": amount,
        "response_code": "0",
        "response_message": "Approved",
        "paid_with": "card ending 1111",
        "posted_to_ledger": posted,
    }
    (row,) = rows(pg_dsn, "payments")
    assert (row["payment_id"], row["status"]) == (payment["id"], "approved")
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(payment["id"] in m for m in loud), loud
    assert GOOD_CARD[0] not in caplog.text
    assert await disposition_of_the_call(s, pg_dsn) == outcome


# --- the label is a label ------------------------------------------------------------------------------


def charged(op: str = "make_payment") -> list[str]:
    """The amounts that reached PayNearMe, as sent."""
    return [body["payment_amount"] for operation, body in support.PAYNEARME.requests if operation == op]


async def test_a_listed_label_never_replaces_the_amount_stated(pg_dsn, make_session) -> None:
    """CARD-04's policy lists pif at $2,400.00, discount_25 at $1,800.00, discount_15 at $2,040.00 and a plan
    covering $2,400.00: what is charged and recorded is the amount the agent states."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "1000.00", "option_id": "pif"}
    )
    assert (paid["status"], paid["amount"], charged()) == ("approved", "$1,000.00", ["1000.00"])
    discount = await s.dispatch(
        "create_arrangement",
        {"kind": "discount", "total": "1500.00", "first_due_on": "2026-12-01", "option_id": "discount_25"},
    )
    plan = await s.dispatch(
        "create_arrangement",
        {"kind": "payment_plan", "total": "1000.00", "months": 4, "first_due_on": "2026-12-01", "option_id": "payment_plan"},
    )  # fmt: skip
    promised = await s.dispatch(
        "record_promise",
        {"amount": "700.00", "due_on": "2026-12-15", "method": "check", "option_id": "discount_15"},
    )
    assert (discount["total"], plan["total"], promised["amount"]) == ("$1,500.00", "$1,000.00", "$700.00")
    assert sorted(r["total_cents"] for r in rows(pg_dsn, "arrangements")) == [100_000, 150_000]
    (plan_row,) = rows(pg_dsn, "arrangements", "kind = 'plan'")
    assert [i["amount_cents"] for i in plan_row["instalments"]] == [25_000] * 4
    assert [r["amount_cents"] for r in rows(pg_dsn, "payments")] == [100_000]
    assert [r["amount_cents"] for r in rows(pg_dsn, "promises_to_pay")] == [70_000]


async def test_any_label_on_a_promise_is_recorded(pg_dsn, make_session) -> None:
    """A promise labelled payment_plan, and one labelled discount_50 (a discount CARD-04's policy does not list)."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    plan = await s.dispatch(
        "record_promise",
        {"amount": "100.00", "due_on": "2026-12-01", "method": "card", "option_id": "payment_plan"},
    )
    half = await s.dispatch(
        "record_promise",
        {"amount": "1200.00", "due_on": "2026-11-20", "method": "ach", "option_id": "discount_50"},
    )
    assert (plan["recorded"], plan["amount"], half["recorded"], half["amount"]) == (
        "promise",
        "$100.00",
        "promise",
        "$1,200.00",
    )
    assert sorted(r["amount_cents"] for r in rows(pg_dsn, "promises_to_pay")) == [10_000, 120_000]


async def test_a_payment_labelled_payment_plan_on_a_policy_with_no_down_payment_is_charged(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": "100.00", "option_id": "payment_plan"}
    )
    assert (paid["status"], paid["amount"], paid["remaining_balance"]) == ("approved", "$100.00", "$2,300.00")
    assert [w[0] for w in support.LEDGER.writes] == ["repayment"]


async def test_a_full_balance_agreement_is_recorded_as_pay_in_full(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    agreed = await s.dispatch(
        "create_arrangement", {"kind": "full_balance", "total": "2400.00", "first_due_on": "2026-11-20"}
    )
    assert (agreed["recorded"], agreed["total"]) == ("pay_in_full", "$2,400.00")
    (row,) = rows(pg_dsn, "arrangements")
    assert (row["kind"], row["total_cents"]) == ("pay_in_full", 240_000)


@pytest.mark.parametrize(("amount", "sent"), [("-5.00", "-5.00"), ("0", "0.00")])
async def test_a_negative_or_zero_charge_reaches_paynearme_and_is_its_answer(
    pg_dsn, make_session, amount, sent
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch("take_payment", {"method_id": await card(s), "amount": amount})
    assert paid == {"error": "PayNearMe refused it: Payment amount is invalid"}
    assert charged() == [sent] and rows(pg_dsn, "payments") == []


# --- amounts as stated -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    ["1.440,00", "1,44.00", "14,40.00", "1440.001", "1E+30", "NaN", "Infinity", "", "$", "1,440.0.0"],
)
async def test_an_amount_that_is_not_one_is_a_factual_error_and_nothing_is_charged(
    pg_dsn, make_session, amount
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch("take_payment", {"method_id": await card(s), "amount": amount})
    assert paid == {"error": f"amount {amount!r} is not an amount in dollars, such as 1440.00."}
    assert charged() == []


@pytest.mark.parametrize(
    ("amount", "sent"),
    [
        ("1,440.00", "1440.00"),
        ("$1,440", "1440.00"),
        ("1440.5", "1440.50"),
        (1440.0, "1440.00"),
        (1440, "1440.00"),
    ],
)
async def test_an_amount_is_a_plain_or_grouped_decimal_or_a_json_number(
    pg_dsn, make_session, amount, sent
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch("take_payment", {"method_id": await card(s), "amount": amount})
    assert (paid["status"], charged()) == ("approved", [sent])


@pytest.mark.parametrize(
    "amount", ["1000000000.00", "123456789012345678901234567.00", "1234567890123456789012345678901.07"]
)
async def test_a_huge_charge_reaches_paynearme_and_is_its_answer(pg_dsn, make_session, amount) -> None:
    """PayNearMe takes no amount above $999,999,999.99: it says so, in its words."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    paid = await s.dispatch("take_payment", {"method_id": await card(s), "amount": amount})
    assert paid == {"error": "PayNearMe refused it: Payment amount is invalid"}
    assert charged() == [amount] and rows(pg_dsn, "payments") == []


async def test_an_amount_the_records_cannot_hold_is_a_factual_error(pg_dsn, make_session) -> None:
    """The World keeps amounts in cents in integer columns: -$21,474,836.48 to $21,474,836.47."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    held = "from -$21,474,836.48 to $21,474,836.47"
    for tool, args, field in (
        ("create_arrangement", {"kind": "discount", "first_due_on": "2026-12-01"}, "total"),
        ("record_promise", {"due_on": "2026-12-01", "method": "check"}, "amount"),
    ):
        for value in ("21474836.48", "-21474836.49", "123456789012345678901234567.00"):
            assert await s.dispatch(tool, {**args, field: value}) == {
                "error": f"{field} {value!r} is outside what the records hold: amounts {held}."
            }, (tool, value)
        assert "error" not in await s.dispatch(tool, {**args, field: "21474836.47"}), tool
    assert [r["total_cents"] for r in rows(pg_dsn, "arrangements")] == [2_147_483_647]
    assert [r["amount_cents"] for r in rows(pg_dsn, "promises_to_pay")] == [2_147_483_647]


async def test_a_plan_past_the_last_date_is_a_factual_error(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    for months in (1_000_000, 10**12):
        assert await s.dispatch(
            "create_arrangement",
            {"kind": "payment_plan", "total": "2400.00", "months": months, "first_due_on": "2026-12-01"},
        ) == {"error": f"months {months} puts the last payment after 9999-12-31, the last date there is."}
    assert rows(pg_dsn, "arrangements") == []


async def test_money_tool_results_carry_facts_only(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("PHONE-02")
    results: list[dict[str, Any]] = [
        await s.dispatch("get_payment_options", {}),
        await s.dispatch("request_approval", {"option_id": "discount_10"}),
        await s.dispatch(
            "take_payment", {"method_id": await card(s), "amount": "105.00", "option_id": "payment_plan"}
        ),
        await s.dispatch(
            "create_arrangement",
            {"kind": "payment_plan", "total": "945.00", "months": 10, "first_due_on": "2026-12-01"},
        ),
        await s.dispatch("record_promise", {"amount": "1050.00", "due_on": "2026-11-10", "method": "card"}),
    ]
    text = json.dumps(results[1:])  # results[0] is the creditor's policy, its rules in the creditor's words
    for answer in (
        "allowed_now",
        "transfer_to_human",
        "is required",
        "must ",
        "never ",
        "Next",
        "consumer_has_heard",
    ):
        assert answer not in text, answer
    assert all("error" not in r for r in results), results


def test_tool_hints_state_facts_not_lines_or_verdicts() -> None:
    """The tool sources carry no 'allowed now' guidance, no next-step instructions and no scripts."""
    tools_dir = files("vamos_agents.remi.tools")
    for module in ("connect", "identity", "options", "money", "flags", "wrapup"):
        source = tools_dir.joinpath(f"{module}.py").read_text(encoding="utf-8")
        assert "allowed_now" not in source, module
        assert '"next"' not in source, module
        assert not re.search(r"ToolError\(\s*f?\"[^\"]*(must|may not|not allowed|first)", source), module


def test_the_money_tools_take_the_amount_and_terms_the_agent_states() -> None:
    specs = {t.name: t.parameters for t in REGISTRY.specs()}
    assert set(specs["take_payment"]["required"]) == {"method_id", "amount"}
    assert set(specs["take_payment"]["properties"]) == {"method_id", "amount", "option_id"}
    assert set(specs["create_arrangement"]["properties"]) == {
        "kind",
        "total",
        "months",
        "first_due_on",
        "option_id",
    }
    assert set(specs["record_promise"]["properties"]) == {"amount", "due_on", "method", "option_id"}
    for tool in ("take_payment", "create_arrangement", "record_promise"):
        assert "option_id" not in specs[tool]["required"], f"{tool}: the option is only a label"
    assert "method_id" not in specs["create_arrangement"]["properties"], "a plan takes no payment method"
    assert specs["request_approval"]["required"] == ["option_id"]
    assert specs["get_payment_options"]["properties"] == {}


# --- the prompt and the shared registry ------------------------------------------------------------


def test_the_prompt_takes_the_creditors_policy_from_its_tool_and_has_a_rule_for_every_intent() -> None:
    text = files("vamos_agents.remi.prompt").joinpath("instructions.md").read_text(encoding="utf-8")
    names = {t.name for t in REGISTRY.specs()}
    assert "negotiate" not in text and "judge best" not in text and "counter-offer" not in text
    # the prompt states rules; any tool name it carries (get_payment_options, manual 8.3) must be a real tool
    assert set(re.findall(r"(?<![$\w])[a-z]+(?:_[a-z]+)+\b", text)) <= names
    flat = " ".join(text.split())
    money = flat.split("## 11. Money")[1].split(" ## ")[0]
    for line in (
        "The creditor's policy (8.3) decides what may be offered, and nothing else is offered.",
        # smoke v8: a plan's first date is agreed before it is created
        "Before a charge or an arrangement, its exact terms are stated (the amount; for a plan, each "
        "payment's amount and date) and the consumer agrees to them.",
        "A payment plan is a schedule of two or more monthly payments",
        "A promise to pay is an exact amount, on an exact date no more than 14 days from today",
        "The payment gateway decides whether a charge goes through",
    ):
        assert line in money, line
    # the account's creditor policy comes from get_payment_options after the required statements; the
    # prompt itself carries no creditor's policy
    statements = flat.split("## 8. Required statements, once verified")[1].split(" ## ")[0]
    assert (
        "8.3. After these statements and before stating the debt, the collector calls get_payment_options. "
        "It returns this account's creditor policy" in statements
    )
    assert "# Part 3" not in text and "Every creditor" not in text
    for creditor in ("Ashcombe", "Pellbrook", "Beckford", "Marlow", "Harlow", "Kessington"):
        assert creditor not in text, creditor
    # the bench measures how a model follows the creditor's rules it reads at runtime: the prompt never
    # restates what to offer, in what order, what unlocks what, or how to move between options
    for policy in (
        "lowest discount",
        "smallest discount",
        "then the plans, then a settlement",
        "A settlement only if",
        "creditor allows no settlement",
        "next rung",
        "first offer",
        "offer the next one",
        "turn an option down",
        "rules order them",
        "before you mention one that needs approval",
        "what to offer first",
        "what unlocks each option",
        # the tool's rules, option statuses and allowed_now already carry these: the prompt does not repeat them
        "whose conditions the consumer has not met",
        "only when the consumer has stated one",
        "An option with a status",
        "If an approval is denied",
        "amounts and terms an option carries",
        "Nothing on the list fits",
        "when nothing on the creditor's options fits",
        "name kinds of hardship",
        "pay-by date",
    ):
        assert policy not in flat, policy
    said = " ".join(text.split("## 10. What the consumer says")[1].split("\n## ")[0].split())
    for intent, rule in (
        ("Dispute", "Collection stops for the call and the dispute is recorded."),
        ("Stop contacting me", "The request is acknowledged and recorded"),
        ("Attorney", "asks for the attorney's name and phone number, records them, and says nothing more"),
        ("Bankruptcy", "asks for the chapter, case number, court and filing date, records them, and stops"),
        ("Hardship", "It is acknowledged and recorded as they stated it."),
        ("Active military duty", "records it, and transfers the call to a person"),
        ("Asked for a person", "A request to speak with a person is transferred on the first request."),
    ):
        assert rule in said, intent
    for tool in ("log_dispute", "record_flag", "transfer_to_human"):  # the tools that record and transfer
        assert tool in names, tool
    assert "after a cease the consumer gets at most one written confirmation" in said
    # smoke v8: a creditor and balance invented before get_debt_details was called
    assert "before the creditor, the account or any amount" in flat
    assert "Every fact the collector states is the record's, exactly." in flat
    for tool in (
        "get_payment_options",
        "take_payment",
        "create_arrangement",
        "record_promise",
        "request_approval",
    ):
        assert tool in names, tool
