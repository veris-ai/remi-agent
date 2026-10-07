"""The tools and the session gate against a real Postgres seeded with WORLD.md's schema.

Each flow asserts the exact rows the call leaves, in the shape the benchmark's
answer key (``policy.simulate`` in utility-bench) expects for that case.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from tests.guards import assert_no_wall_clock
from vamos_voice.session import CallSession

from .support import DECLINING_CARD, GOOD_CARD, all_text, rows, seed_world

pytestmark = pytest.mark.postgres

DISCLOSED = {
    "recording_disclosure_given": True,
    "mini_miranda_given": True,
    "alternative_format_statement_given": True,
}
VERIFY = {"full_name": "Aaliyah Alvarez", "date_of_birth": "1958-01-01", "zip": "11215"}


async def open_call(s: CallSession) -> dict:
    for name in ("get_account", "get_dialed_phone"):
        assert "error" not in await s.dispatch(name, {})
    return await s.dispatch("get_contact_history", {})


async def verified(s: CallSession) -> None:
    await open_call(s)
    assert (await s.dispatch("verify_right_party", VERIFY))["verified"] is True


async def unlocked(s: CallSession) -> dict:
    await verified(s)
    assert "error" not in await s.dispatch("set_language", {"language": "en"})
    details = await s.dispatch("get_debt_details", DISCLOSED)
    assert "error" not in details, details
    return details


async def card(s: CallSession, number: str = GOOD_CARD) -> str:
    pm = await s.dispatch(
        "add_payment_method",
        {"method": "card", "card_number": number, "exp_month": 12, "exp_year": 30, "cvc": "123"},
    )
    return pm["payment_method_id"]


def strip(row: dict, *ignore: str) -> dict:
    return {k: v for k, v in row.items() if k not in ignore}


# --- the gate -------------------------------------------------------------------------------


async def test_nothing_before_the_connect_reads(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    for name, args in (
        ("verify_right_party", VERIFY),
        ("get_debt_details", DISCLOSED),
        ("log_contact_attempt", {"answered_by": "none", "summary": "nothing yet here"}),
    ):
        assert "connect-time reads" in (await s.dispatch(name, args))["error"]


async def test_debt_tools_wait_for_verification_and_disclosures(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    assert "right party" in (await s.dispatch("get_debt_details", DISCLOSED))["error"]
    assert (
        "right party"
        in (
            await s.dispatch(
                "take_payment", {"payment_method_id": "x", "amount_cents": 1, "purpose": "pay_in_full"}
            )
        )["error"]
    )
    await s.dispatch("verify_right_party", VERIFY)
    refused = await s.dispatch(
        "get_debt_details", {"recording_disclosure_given": True, "mini_miranda_given": True}
    )
    assert "language question" in refused["error"] and "alternative-format" in refused["error"]
    assert (
        "disclosures"
        in (
            await s.dispatch(
                "take_payment", {"payment_method_id": "x", "amount_cents": 1, "purpose": "pay_in_full"}
            )
        )["error"]
    )


async def test_connect_reads_return_no_secrets_and_assess_the_call(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    account = await s.dispatch("get_account", {})
    text = str(account)
    for secret in ("1958", "11215", "2234", "7th Ave", "18500", "Ashcombe", "4001"):
        assert secret not in text
    assert "call_assessment" not in account
    last = await open_call(s)
    assert last["call_assessment"]["proceed"] is True
    assert last["call_assessment"]["first_conversation"] is True
    assert s.tool_log[:3] == ["get_account", "get_account", "get_dialed_phone"]


async def test_should_not_call_closes_without_verification(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, phone={"source": "employer"})
    s = await make_session()
    assessment = (await open_call(s))["call_assessment"]
    assert assessment["proceed"] is False and assessment["should_not_call_reasons"] == ["employer_phone"]
    assert "should not have been placed" in (await s.dispatch("verify_right_party", VERIFY))["error"]
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "consumer", "summary": "Closed the call at connect."}
    )
    assert logged["disposition"] == "should_not_call"
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert attempt["answered_by"] is None and attempt["consumer_responded"] is False
    assert (await s.dispatch("end_call", {}))["ended"] is True


async def test_three_failed_verifications_lock_the_call(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    wrong = {"full_name": "Aaliyah Alvarez", "date_of_birth": "1958-02-01", "zip": "11201"}
    assert (
        "not counted" in (await s.dispatch("verify_right_party", {"full_name": "Aaliyah Alvarez"}))["error"]
    )
    results = [await s.dispatch("verify_right_party", wrong) for _ in range(3)]
    assert [r["attempts_left"] for r in results] == [2, 1, 0]
    assert "three" in (await s.dispatch("verify_right_party", VERIFY))["error"]
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "none", "summary": "Caller could not verify identity."}
    )
    assert logged["disposition"] == "failed_verification"


# --- money -----------------------------------------------------------------------------------


async def test_pay_in_full_by_card_leaves_exactly_the_answer_key_rows(
    pg_dsn, make_session, payments, ticks, caplog
) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    caplog.set_level(logging.INFO)
    details = await unlocked(s)
    assert details["current_balance_cents"] == 18500
    paid = await s.dispatch(
        "take_payment", {"payment_method_id": await card(s), "amount_cents": 18500, "purpose": "pay_in_full"}
    )
    assert paid["status"] == "succeeded"
    ticks.t += 125
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "consumer", "summary": "Consumer paid the full balance by card."},
    )

    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert strip(attempt, "attempt_id", "summary", "agent_name", "account_id") == {
        "phone_id": "ph_420101_1",
        "at": datetime.fromisoformat("2027-02-10T09:00:00-05:00"),
        "medium": "call",
        "direction": "outbound",
        "answered_by": "consumer",
        "consumer_responded": True,
        "duration_s": 125,
        "disposition": "paid",
    }
    assert rows(pg_dsn, "consumers")[0]["language_preference"] == "en"
    (payment,) = rows(pg_dsn, "payments")
    assert (payment["amount_cents"], payment["status"], payment["stripe_payment_intent_id"]) == (
        18500,
        "succeeded",
        "pi_test_1",
    )
    assert payment["at"].year == 2027
    account = rows(pg_dsn, "accounts")[0]
    assert (account["current_balance_cents"], account["paid_since_charge_off_cents"], account["status"]) == (
        0,
        18500,
        "paid",
    )
    assert account["last_payment_on"] == date(2027, 2, 10) and account["stripe_customer_id"] == "cus_test_1"
    for table in ("account_flags", "arrangements", "promises_to_pay", "callbacks", "letters"):
        assert rows(pg_dsn, table) == [], table
    assert GOOD_CARD not in all_text(pg_dsn)
    assert GOOD_CARD not in caplog.text and "[redacted]" in caplog.text


async def test_decline_is_recorded_and_then_a_promise(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    failed = await s.dispatch(
        "take_payment",
        {"payment_method_id": await card(s, DECLINING_CARD), "amount_cents": 18500, "purpose": "pay_in_full"},
    )
    assert failed["status"] == "failed" and "did not go through" in failed["next"]
    assert (
        await s.dispatch("record_promise", {"amount_cents": 18500, "due_on": "2027-02-25", "method": "card"})
    )["error"]
    assert (
        await s.dispatch("record_promise", {"amount_cents": 18500, "due_on": "2027-02-19", "method": "card"})
    )["recorded"]
    assert (
        await s.dispatch(
            "log_contact_attempt",
            {"answered_by": "consumer", "summary": "Card declined; consumer promised to pay."},
        )
    )["disposition"] == "ptp"
    assert [p["status"] for p in rows(pg_dsn, "payments")] == ["failed"]
    account = rows(pg_dsn, "accounts")[0]
    assert (
        account["status"] == "active"
        and account["current_balance_cents"] == 18500
        and account["stripe_customer_id"]
    )


async def test_settlement_below_the_floor_goes_to_a_person(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, account={"current_balance_cents": 200000, "charge_off_balance_cents": 200000})
    s = await make_session()
    await unlocked(s)
    pm = await card(s)
    refused = await s.dispatch(
        "take_payment", {"payment_method_id": pm, "amount_cents": 80000, "purpose": "settlement"}
    )
    assert "transfer_to_human" in refused["error"]
    await s.dispatch("transfer_to_human", {"reason": "settlement_below_floor"})
    assert (
        await s.dispatch(
            "log_contact_attempt", {"answered_by": "consumer", "summary": "Wanted 40 percent; transferred."}
        )
    )["disposition"] == "transferred"
    assert rows(pg_dsn, "payments") == [] and rows(pg_dsn, "arrangements") == []


async def test_lump_settlement_writes_payment_arrangement_and_letter(pg_dsn, make_session) -> None:
    seed_world(
        pg_dsn,
        account={
            "current_balance_cents": 100000,
            "charge_off_balance_cents": 100000,
            "settlement_floor_pct": 70,
        },
    )
    s = await make_session()
    await unlocked(s)
    ok = await s.dispatch(
        "take_payment", {"payment_method_id": await card(s), "amount_cents": 75000, "purpose": "settlement"}
    )
    assert ok["status"] == "succeeded"
    (arr,) = rows(pg_dsn, "arrangements")
    assert (arr["kind"], arr["total_cents"], arr["instalments"]) == (
        "settlement_lump",
        75000,
        [{"due_on": "2027-02-10", "amount_cents": 75000}],
    )
    assert arr["confirmation_letter_due_by"] == date(2027, 2, 18)
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["plan_confirmation"]
    account = rows(pg_dsn, "accounts")[0]
    assert (account["status"], account["current_balance_cents"]) == ("settled", 25000)


async def test_hardship_plan_needs_the_hardship_recorded_first(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, account={"current_balance_cents": 96000, "charge_off_balance_cents": 96000})
    s = await make_session()
    await unlocked(s)
    pm = await card(s)
    plan = {
        "kind": "hardship_plan",
        "payment_method_id": pm,
        "instalment_count": 24,
        "first_due_on": "2027-03-01",
    }
    assert "Record the hardship first" in (await s.dispatch("create_arrangement", plan))["error"]
    await s.dispatch("record_flag", {"kind": "hardship", "hardship_reason": "job_loss"})
    made = await s.dispatch("create_arrangement", plan)
    assert len(made["instalments"]) == 24 and made["instalments"][0] == {
        "due_on": "2027-03-01",
        "amount_cents": 4000,
    }
    assert (
        await s.dispatch(
            "log_contact_attempt", {"answered_by": "consumer", "summary": "Lost job; set up a hardship plan."}
        )
    )["disposition"] == "hardship_plan"
    assert [f["details"] for f in rows(pg_dsn, "account_flags")] == [{"reason": "job_loss"}]


# --- signals ---------------------------------------------------------------------------------


async def test_dispute_stops_collection(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    await s.dispatch("log_dispute", {"reason": "not_mine"})
    refused = await s.dispatch(
        "add_payment_method",
        {"method": "card", "card_number": GOOD_CARD, "exp_month": 1, "exp_year": 2030, "cvc": "123"},
    )
    assert "Collection stopped" in refused["error"]
    assert (
        await s.dispatch(
            "log_contact_attempt",
            {"answered_by": "consumer", "summary": "Consumer says the debt is not theirs."},
        )
    )["disposition"] == "dispute"
    assert rows(pg_dsn, "accounts")[0]["status"] == "disputed"
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["substantiation_instructions"]


async def test_cease_revokes_consent_on_the_worlds_clock(pg_dsn, make_session, ticks) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await verified(s)
    ticks.t += 30
    await s.dispatch("record_flag", {"kind": "cease"})
    phone = rows(pg_dsn, "phones")[0]
    assert phone["consent_revoked_at"] == datetime.fromisoformat("2027-02-10T09:00:30-05:00")
    assert rows(pg_dsn, "accounts")[0]["status"] == "cease"
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["cease_confirmation"]


async def test_deceased_reported_by_the_executor(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, consumer={"estate_rep_name": "Simone Achterberg"})
    s = await make_session()
    await open_call(s)
    await s.dispatch(
        "record_flag",
        {
            "kind": "deceased",
            "reported_by": "Simone Achterberg",
            "relationship": "child",
            "died_on": "2026-12-30",
        },
    )
    assert (await s.dispatch("verify_right_party", {"full_name": "Simone Achterberg", "role": "estate_rep"}))[
        "verified"
    ]
    details = await s.dispatch(
        "get_debt_details", {"recording_disclosure_given": True, "mini_miranda_given": True}
    )
    assert "limits" not in details and details["current_balance_cents"] == 18500
    assert (
        "Collection stopped"
        in (
            await s.dispatch(
                "record_promise", {"amount_cents": 100, "due_on": "2027-02-12", "method": "check"}
            )
        )["error"]
    )
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "estate_rep", "summary": "Executor reported the death."}
    )
    assert logged["disposition"] == "deceased"
    (flag,) = rows(pg_dsn, "account_flags")
    assert flag["details"] == {
        "reported_by": "Simone Achterberg",
        "relationship": "child",
        "is_estate_rep": True,
    }
    assert rows(pg_dsn, "consumers")[0]["deceased_on"] == date(2026, 12, 30)


async def test_voicemail_and_wrong_number(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "voicemail", "summary": "Left a limited-content message."}
    )
    assert logged["disposition"] == "voicemail"
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert (attempt["medium"], attempt["answered_by"]) == ("voicemail", "voicemail")
    seed_world(pg_dsn)
    s2 = await make_session()
    await open_call(s2)
    await s2.dispatch(
        "log_contact_attempt",
        {"answered_by": "wrong_number", "summary": "Wrong number; apologised.", "number_is_wrong": True},
    )
    assert rows(pg_dsn, "phones")[0]["status"] == "wrong_number"


async def test_callbacks_respect_the_window(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await verified(s)
    late = await s.dispatch(
        "schedule_callback", {"scheduled_for": "2027-02-11T21:30:00-05:00", "consumer_requested": True}
    )
    assert "9:00 p.m." in late["error"]
    agency = await s.dispatch(
        "schedule_callback", {"scheduled_for": "2027-02-12T10:00:00-05:00", "consumer_requested": False}
    )
    assert "7 days" in agency["error"]
    ok = await s.dispatch(
        "schedule_callback", {"scheduled_for": "2027-02-17T10:00:00", "consumer_requested": True}
    )
    assert ok["scheduled_for"] == "2027-02-17T10:00:00-05:00"


async def test_hang_up_before_logging_still_leaves_one_row(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await verified(s)
    assert "log_contact_attempt first" in (await s.dispatch("end_call", {}))["error"]
    await s.finalize()
    await s.finalize()
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert (attempt["answered_by"], attempt["disposition"]) == ("consumer", "no_commitment")


async def test_time_barred_call_requeues_the_notice(pg_dsn, make_session) -> None:
    seed_world(
        pg_dsn, account={"sol_expires_on": date(2026, 6, 1), "time_barred_notice_mailed_on": date(2027, 1, 1)}
    )
    s = await make_session()
    assert (await open_call(s))["call_assessment"]["time_barred"] is True
    await s.dispatch("verify_right_party", VERIFY)
    await s.dispatch("set_language", {"language": "en"})
    assert "time-barred" in (await s.dispatch("get_debt_details", DISCLOSED))["error"]
    assert "error" not in await s.dispatch(
        "get_debt_details", {**DISCLOSED, "time_barred_disclosure_given": True}
    )
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "consumer", "summary": "Gave the time-barred disclosure; no commitment."},
    )
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["time_barred"]


def test_tool_timestamps_never_come_from_wall_time() -> None:
    """Static guard: no Remi code calls a wall clock, and no SQL it sends uses now()."""
    from agents.remi import agent, call, context, db, gates, payments, policy, prompt
    from agents.remi.tools import connect, flags, identity, money, wrapup

    for module in (
        agent,
        call,
        context,
        db,
        gates,
        payments,
        policy,
        prompt,
        connect,
        flags,
        identity,
        money,
        wrapup,
    ):
        assert_no_wall_clock(module)
