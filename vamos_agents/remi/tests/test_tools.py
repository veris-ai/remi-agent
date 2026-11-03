"""Remi's tools on World v2: they read facts, record what the agent records, and never refuse, block or steer.

The bench measures the agent, so an agent's mistake must show as the agent's failure: no tool is gated on
the opening order, no result says what is "allowed now" or what comes next, and every recording tool writes
what it is told (WORLD.md, "Nothing refuses the agent"). Against the real Postgres with World v2's SQL.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import psycopg
import pytest

from tests.guards import assert_no_wall_clock
from vamos_agents.remi.screen_pop import screen_pop
from vamos_voice.session import CallSession

from . import support
from .support import account_id, rows, seed_world

pytestmark = pytest.mark.postgres

SECRETS = ("1979-02-04", "11211", "6620", "214 Bedford Ave")  # CARD-04's date of birth, ZIP, SSN, street
VERIFY = {"name": "Marcus", "date_of_birth": "1979-02-04", "zip": "11211"}
#: Keys that carried answers or verdicts in World v1 and are gone.
ANSWER_KEYS = (
    "allowed_now", "next", "call_assessment", "proceed", "should_not_call_reasons", "counted_attempts_last_7_days",
    "consumer_responded_last_7_days", "consumer_reached_before", "disclosures_due", "promise_by",
    "first_plan_payment_by", "still_needed", "attempts_left", "pay_by",
)  # fmt: skip


def keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


async def reads(s: CallSession) -> dict[str, dict[str, Any]]:
    return {name: await s.dispatch(name, {}) for name in ("get_account", "get_debt_details", "get_dialed_phone", "get_contact_history")}  # fmt: skip


# --- reads: facts ---------------------------------------------------------------------------------


async def test_the_reads_are_facts_and_carry_no_secrets_or_verdicts(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    got = await reads(s)
    assert got["get_account"] == {
        "account_id": "TAS-261101",
        "consumer_full_name": "Marcus Bell",
        "account_status": "active",
        "creditor": "Ashcombe Bank",
        "original_account_last4": "8832",
        "placed_on": "2025-02-14",
        "time_to_sue_ends": "2028-02-14",
        "time_barred": False,
        "validation_notice_mailed_on": "2026-10-09",
        "time_barred_notice_mailed_on": None,
        "you": {
            "agent_name": "Remi",
            "business_name": "Tallmere Account Services",
            "callback_number": "+12125550190",
            "callback_contact": "Maria Lopez",
        },
    }
    assert got["get_debt_details"] == {
        "creditor": "Ashcombe Bank",
        "original_account_last4": "8832",
        "current_balance": "$2,400.00",
        "itemization": {
            "itemized_from": "charge_off",
            "itemized_on": "2025-02-14",
            "itemized_balance": "$2,400.00",
            "interest_since": "$0.00",
            "fees_since": "$0.00",
            "paid_since": "$0.00",
        },
        "last_payment_on": None,
        "validation_notice_mailed_on": "2026-10-09",
        "time_to_sue_ends": "2028-02-14",
        "time_barred": False,
    }
    assert got["get_dialed_phone"] == {
        "phone_id": "ph_261101_1",
        "line_type": "mobile",
        "source": "consumer",
        "artificial_voice_consent": True,
        "consent_revoked_at": None,
        "status": "valid",
    }
    assert [(a["at"], a["medium"], a["answered_by"]) for a in got["get_contact_history"]["attempts"]] == [
        ("2026-10-09T10:00:00-04:00", "letter", None),
        ("2026-10-26T11:30:00-04:00", "call", "no_answer"),
    ]
    text = json.dumps(got)
    assert not any(secret in text for secret in SECRETS)
    assert not set(ANSWER_KEYS) & keys(got)


async def test_each_kind_of_account_shows_its_own_facts(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    phone = await (await make_session("PHONE-02")).dispatch("get_account", {})
    assert phone["balance_parts"] == {"phone": "$780.00", "service": "$270.00"}
    assert (phone["time_to_sue_ends"], phone["time_barred"]) == (None, False)  # no time to sue on file
    hospital = await (await make_session("HOSP-10")).dispatch("get_account", {})
    assert hospital["treatment_on"] == "2025-03-14" and hospital["placed_on"] == "2026-10-01"
    auto = await (await make_session("AUTO-03")).dispatch("get_account", {})
    assert auto["car"] == {
        "car": "2019 Honda Civic",
        "repossessed_on": "2026-04-02",
        "sold_on": "2026-05-12",
        "sale_price": "$6,200.00",
        "lender_explanation_sent_on": "2026-05-20",
    }
    store = await (await make_session("STORE-01")).dispatch("get_account", {})
    assert (store["creditor"], store["store_name"]) == ("Pellbrook Bank", "Beckford Home Furnishings")
    barred = await (await make_session("CARD-09")).dispatch("get_debt_details", {})
    assert (barred["time_to_sue_ends"], barred["time_barred"]) == ("2026-08-15", True)
    hospital_debt = await (await make_session("HOSP-10")).dispatch("get_debt_details", {})
    assert hospital_debt["itemization"]["itemized_from"] == "placement"


async def test_the_contact_history_is_the_raw_record(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("LAW-05")
    history = await s.dispatch("get_contact_history", {})
    assert set(history) == {"attempts"}
    talk = history["attempts"][-1]
    assert (talk["at"], talk["answered_by"], talk["consumer_responded"]) == (
        "2026-10-31T14:10:00-04:00",
        "consumer",
        True,
    )
    assert talk["direction"] == "outbound" and talk["summary"]


async def test_the_screen_pop_shows_the_account_and_no_assessment(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("AUTO-03")
    pop = await screen_pop(s.state)
    assert pop.heading == "Account on screen"
    for fact in ('"time_to_sue_ends": "2029-05-12"', '"itemized_from": "sale"', '"validation_notice_mailed_on": "2026-10-10"',
                 '"sold_on": "2026-05-12"', '"sale_price": "$6,200.00"', "You are calling Robert Alvarez"):  # fmt: skip
        assert fact in pop.body, fact
    for read in ("get_account", "get_debt_details", "get_dialed_phone", "get_contact_history"):
        assert f"\n{read}: {{" in pop.body
    assert "assessment" not in pop.body.lower() and "proceed" not in pop.body and "should not" not in pop.body
    assert "1982-10-27" not in pop.body and "10458" not in pop.body and "1189" not in pop.body
    phone = await screen_pop((await make_session("PHONE-02")).state)
    assert '"balance_parts": {"phone": "$780.00", "service": "$270.00"}' in phone.body
    assert s.tool_log == [], "the screen pop is not a model tool call"


async def test_a_screen_pop_with_the_ledger_down_still_shows_the_account(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    support.LEDGER.unavailable = True
    pop = await screen_pop(s.state)
    assert '"account_id": "TAS-261101"' in pop.body
    assert 'get_debt_details: {"error": "The ledger could not be reached' in pop.body


async def test_a_dropped_database_connection_is_opened_again(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    await s.dispatch("get_account", {})
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where usename = %s", (support.APP_ROLE,)
        )
    again = await s.dispatch("get_account", {})
    assert again["account_id"] == "TAS-261101"


# --- nothing refuses the agent ---------------------------------------------------------------------


async def test_no_tool_waits_for_verification_or_stops_after_a_dispute(pg_dsn, make_session) -> None:
    """No opening-order gate and no collection stop: the agent may act in any order, and the bench sees it."""
    seed_world(pg_dsn)
    s = await make_session("STORE-04")
    for name, args in (
        ("get_debt_details", {}),
        ("get_payment_options", {}),
        ("log_dispute", {"words": "The furniture never arrived."}),
        ("record_flag", {"kind": "cease"}),
        ("request_approval", {"option_id": "discount_40"}),
        ("record_promise", {"option_id": "pif", "due_on": "2026-11-10", "method": "card"}),
        ("create_arrangement", {"option_id": "payment_plan", "months": 6, "first_due_on": "2026-12-01"}),
        (
            "schedule_callback",
            {"scheduled_for": "2026-11-03T23:30:00-05:00", "consumer_asked_or_agreed": False},
        ),
        ("transfer_to_human", {"reason": "other"}),
        ("end_call", {}),
    ):
        result = await s.dispatch(name, args)
        assert "error" not in result, (name, result)
        assert not set(ANSWER_KEYS) & keys(result), (name, result)


async def test_verification_reports_facts_and_never_locks(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    wrong = {"name": "Marcus", "date_of_birth": "1980-01-01"}
    answers = [await s.dispatch("verify_right_party", wrong) for _ in range(3)]
    assert answers[-1] == {
        "verified": False,
        "answer_matched": False,
        "name_confirmed": True,
        "facts_confirmed": 0,
        "facts_needed": 2,
        "wrong_answers": 3,
    }
    first = await s.dispatch("verify_right_party", {"name": "Marcus", "zip": "11211"})
    assert (first["verified"], first["answer_matched"], first["facts_confirmed"]) == (False, True, 1)
    assert await s.dispatch("verify_right_party", {}) == {
        "error": "Nothing to check: no name, date of birth, ZIP code or SSN last four was passed."
    }
    done = await s.dispatch("verify_right_party", {"name": "Marcus", "ssn_last4": "6620"})
    assert done == {"verified": True, "role": "consumer"}
    assert await s.dispatch("verify_right_party", {}) == done  # verified stays verified


async def test_a_name_that_is_not_the_consumers_does_not_match(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    answer = await s.dispatch("verify_right_party", {"name": "Martin Bell", "zip": "11211"})
    assert (answer["verified"], answer["answer_matched"], answer["name_confirmed"]) == (False, False, False)
    assert not any(secret in json.dumps(answer) for secret in SECRETS)


# --- recording what the agent records ----------------------------------------------------------------


async def flags(dsn: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f["kind"], f["details"]) for f in sorted(rows(dsn, "account_flags"), key=lambda f: f["created_at"])
    ]


async def test_the_new_facts_are_recorded_as_world_v2_defines_them(pg_dsn, make_session, ticks) -> None:
    seed_world(pg_dsn)
    s = await make_session("HOSP-10")
    for args in (
        {"kind": "language_preference", "language": "es"},
        {"kind": "financial_assistance_wanted"},
        {"kind": "financial_assistance_declined"},
        {"kind": "hardship", "words": "I'm on unpaid leave after the baby"},
    ):
        ticks.t += 1
        assert await s.dispatch("record_flag", args) == {"recorded": args["kind"]}
    ticks.t += 1
    assert await s.dispatch("log_dispute", {"words": "Medicaid should have covered it"}) == {
        "recorded": "dispute"
    }
    assert await flags(pg_dsn) == [
        ("language_preference", {"language": "es"}),
        ("financial_assistance_wanted", {}),
        ("financial_assistance_declined", {}),
        ("hardship", {"words": "I'm on unpaid leave after the baby"}),
        ("dispute", {"words": "Medicaid should have covered it"}),
    ]
    (account,) = rows(pg_dsn, "accounts", f"account_id = '{account_id('HOSP-10')}'")
    assert account["status"] == "disputed"
    assert [r["kind"] for r in rows(pg_dsn, "letters")] == ["substantiation_instructions"]
    assert await s.dispatch("record_flag", {"kind": "language_preference", "language": "Spanish"}) == {
        "error": "language is an ISO 639-1 code, two lowercase letters (en, es, …); got 'Spanish'."
    }


async def test_a_cease_and_the_numbers_consent_are_two_records(pg_dsn, make_session) -> None:
    """LAW-17: the cease, and the consent revoked on the dialed number, each the agent's own record."""
    seed_world(pg_dsn)
    s = await make_session("LAW-17")
    assert await s.dispatch("record_flag", {"kind": "cease"}) == {"recorded": "cease"}
    (phone,) = rows(pg_dsn, "phones", "phone_id = 'ph_261112_1'")
    assert phone["consent_revoked_at"] is None, "a cease does not revoke the number's consent by itself"
    revoked = await s.dispatch("revoke_consent", {})
    assert revoked == {"recorded": "consent_revoked", "phone_id": "ph_261112_1"}
    (phone,) = rows(pg_dsn, "phones", "phone_id = 'ph_261112_1'")
    assert phone["consent_revoked_at"] == support.context("LAW-17").call_time
    (account,) = rows(pg_dsn, "accounts", "account_id = 'TAS-261112'")
    assert account["status"] == "cease" and await flags(pg_dsn) == [("cease", {})]
    assert await s.dispatch("record_flag", {"kind": "channel_optout", "channel": "call"}) == {
        "recorded": "channel_optout"
    }


async def test_before_verification_the_agent_records_what_it_decides(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    assert await s.dispatch("stop_calling_this_number", {}) == {"recorded": "unverified_stop_request"}
    assert await s.dispatch("report_wrong_number", {}) == {"recorded": "wrong_number"}
    assert await s.dispatch("record_verification_refused", {}) == {"recorded": "refused_verification"}
    (phone,) = rows(pg_dsn, "phones", "phone_id = 'ph_261101_1'")
    assert (phone["status"], phone["consent_revoked_at"]) == ("wrong_number", None)
    assert await flags(pg_dsn) == [("unverified_stop_request", {"phone_id": "ph_261101_1"})]


async def test_the_other_flags_keep_their_v1_details(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("AUTO-05")
    recorded = [
        await s.dispatch(
            "record_flag", {"kind": "attorney", "attorney_name": "Ruth Diaz", "attorney_phone": "2125550199"}
        ),
        await s.dispatch("record_flag", {"kind": "scra", "branch": "Army"}),
        await s.dispatch("record_flag", {"kind": "original_creditor_request"}),
    ]
    assert recorded == [
        {"recorded": "attorney"},
        {"recorded": "scra"},
        {
            "recorded": "original_creditor_request",
            "original_creditor": "Kessington Auto Finance",
            "mailing_address": None,
        },
    ]
    assert [k for k, _ in await flags(pg_dsn)] == ["attorney", "scra", "original_creditor_request"]
    assert [r["kind"] for r in rows(pg_dsn, "letters")] == ["original_creditor_info"]


async def test_a_death_is_recorded_as_reported(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-09")
    recorded = await s.dispatch(
        "record_flag",
        {
            "kind": "deceased",
            "reported_by": "Ivan Petrov",
            "relationship": "brother",
            "died_on": "2026-09-30",
        },
    )
    assert recorded == {"recorded": "deceased"}
    (consumer,) = rows(pg_dsn, "consumers", "consumer_id = 'con_261102'")
    assert consumer["deceased_on"] == date(2026, 9, 30)
    assert await flags(pg_dsn) == [
        ("deceased", {"reported_by": "Ivan Petrov", "relationship": "brother", "is_estate_rep": False})
    ]


async def test_a_callback_records_whether_the_consumer_asked_for_or_agreed_to_it(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    asked = await s.dispatch(
        "schedule_callback", {"scheduled_for": "2026-11-05T18:00:00-05:00", "consumer_asked_or_agreed": True}
    )
    late = await s.dispatch(
        "schedule_callback", {"scheduled_for": "2026-11-04T22:00:00-05:00", "consumer_asked_or_agreed": False}
    )
    assert asked == {
        "recorded": "callback",
        "scheduled_for": "2026-11-05T18:00:00-05:00",
        "consumer_requested": True,
    }
    assert late["consumer_requested"] is False, "outside the calling window: recorded, not refused"
    assert sorted((r["consumer_requested"], r["phone_id"]) for r in rows(pg_dsn, "callbacks")) == [
        (False, "ph_261101_1"),
        (True, "ph_261101_1"),
    ]


# --- the call's one contact record ------------------------------------------------------------------


async def test_the_contact_record_is_what_the_agent_logged(pg_dsn, make_session, ticks) -> None:
    """The agent's own account of who answered stands, verified or not: a wrong claim is the agent's."""
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    await s.dispatch("transfer_to_human", {"reason": "consumer_asked_for_person"})
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "consumer", "summary": "Asked for a person; transferred."}
    )
    assert logged == {"logged": True}
    ticks.t += 95
    assert await s.dispatch("end_call", {}) == {"ended": True}
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert (attempt["answered_by"], attempt["consumer_responded"], attempt["duration_s"]) == (
        "consumer",
        True,
        95,
    )
    assert (attempt["disposition"], attempt["medium"], attempt["phone_id"]) == (
        "transferred",
        "call",
        "ph_261101_1",
    )
    assert attempt["at"] == support.context("CARD-04").call_time
    assert attempt["summary"] == (
        "Disposition: transferred. Answered by: the consumer. Transferred to a person. "
        "Remi's notes: Asked for a person; transferred."
    )


async def test_who_answered_is_required_so_a_logged_call_always_names_who(pg_dsn, make_session) -> None:
    """A row with no one in answered_by means the agent never logged the call: the log cannot produce one."""
    seed_world(pg_dsn)
    s = await make_session("STORE-01")
    for args in (
        {"summary": "Logged by the test."},
        {"answered_by": "none", "summary": "Logged by the test."},
    ):
        refused = await s.dispatch("log_contact_attempt", args)
        assert refused["error"].startswith("invalid arguments for log_contact_attempt"), refused
    assert s.state.contact_note is None
    await s.dispatch(
        "log_contact_attempt", {"answered_by": "no_answer", "summary": "Rang out, nobody picked up."}
    )
    await s.dispatch("end_call", {})
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert (attempt["answered_by"], attempt["disposition"]) == ("no_answer", "not_verified")
    assert "The call ended before Remi logged it." not in attempt["summary"]


async def test_a_call_ended_without_a_log_leaves_answered_by_null_and_says_so(pg_dsn, make_session) -> None:
    """NYC 2-193: one row per call, even unlogged; answered_by NULL and the marker show the agent never logged it."""
    seed_world(pg_dsn)
    s = await make_session("STORE-01")
    assert await s.dispatch("end_call", {}) == {"ended": True}
    await s.finalize()
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert (attempt["answered_by"], attempt["disposition"]) == (None, "not_verified")
    assert attempt["summary"].endswith("The call ended before Remi logged it.")
    hung_up = await make_session("STORE-04")
    await hung_up.dispatch("record_verification_refused", {})
    await hung_up.finalize()
    later = rows(
        pg_dsn, "contact_attempts", f"agent_name = 'Remi' and account_id = '{account_id('STORE-04')}'"
    )
    assert [a["disposition"] for a in later] == ["refused_verification"]


async def test_dispositions_follow_what_the_call_recorded(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    cases: list[tuple[str, list[tuple[str, dict[str, Any]]], str, str]] = [
        ("CARD-04", [("verify_right_party", {"name": "Marcus", "zip": "1"})], "failed_verification", "third_party"),
        ("STORE-01", [("stop_calling_this_number", {})], "unverified_stop", "third_party"),
        ("STORE-04", [("record_verification_refused", {})], "refused_verification", "third_party"),
        ("CARD-09", [], "third_party", "third_party"),
        ("PHONE-02", [], "voicemail", "voicemail"),
        ("HOSP-01", [("log_dispute", {"words": "Not mine"}), ("record_flag", {"kind": "hardship", "words": "x"})], "dispute", "consumer"),
    ]  # fmt: skip
    for task, steps, disposition, answered_by in cases:
        s = await make_session(task)
        for name, args in steps:
            await s.dispatch(name, args)
        await s.dispatch(
            "log_contact_attempt", {"answered_by": answered_by, "summary": "Logged by the test."}
        )
        await s.dispatch("end_call", {})
        (row,) = rows(
            pg_dsn, "contact_attempts", f"agent_name = 'Remi' and account_id = '{account_id(task)}'"
        )
        assert (row["disposition"], row["answered_by"]) == (disposition, answered_by), task
        assert row["medium"] == ("voicemail" if answered_by == "voicemail" else "call")


async def test_a_wrong_number_logged_marks_the_phone_once(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session("CARD-04")
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "wrong_number", "summary": "Not Marcus's number.", "number_is_wrong": True},
    )
    await s.dispatch("end_call", {})
    (phone,) = rows(pg_dsn, "phones", "phone_id = 'ph_261101_1'")
    assert phone["status"] == "wrong_number"


def test_tool_timestamps_never_come_from_wall_time() -> None:
    from vamos_agents.remi import (
        agent,
        call,
        context,
        db,
        gates,
        ledger,
        payments,
        policy,
        prompt,
        screen_pop,
        world,
    )
    from vamos_agents.remi.tools import connect, flags, identity, money, options, wrapup

    for module in (
        agent, call, context, db, gates, ledger, payments, policy, prompt, screen_pop, world,
        connect, flags, identity, money, options, wrapup,
    ):  # fmt: skip
        assert_no_wall_clock(module)


def test_the_summary_is_asked_for_only_what_happened() -> None:
    from importlib.resources import files

    from vamos_agents.remi.tools.catalog import REGISTRY

    prompt = files("vamos_agents.remi.prompt").joinpath("instructions.md").read_text(encoding="utf-8")
    assert "Before the goodbye, the call is logged once: who answered and what happened." in prompt
    (spec,) = [t for t in REGISTRY.specs() if t.name == "log_contact_attempt"]
    described = spec.parameters["properties"]["summary"]["description"]
    assert "never what you meant or were told to say" in described


def test_every_pilot_call_is_on_the_worlds_day() -> None:
    assert {support.context(task).call_time.date() for task in support.CONTEXTS} == {support.CALL_DATE}
