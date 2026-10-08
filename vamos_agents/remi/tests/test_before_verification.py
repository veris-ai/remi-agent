"""Before verification: RULES.md's refusal to verify and stop requests from an unverified person.

Unit tests on one seeded account, then scripted sessions of the bench's REMI-128 ("No, I will
not. Send me a letter.") and REMI-134 ("Stop calling me.") on the whole vendored World, checked
against those cases' answer-key postconditions (utility-bench
collections_bench/generated/manifest.json at the commit collections_world.sql names).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from vamos_agents.remi.context import CallContext
from vamos_agents.remi.prompt import render_instructions
from vamos_agents.remi.world import Agency
from vamos_voice.session import CallSession

from .support import CALL_TIME, changes, rows, seed_full_world, seed_world, snapshot
from .test_tools import DISCLOSED, IGNORED, VERIFY, comparable, open_call, verified

pytestmark = pytest.mark.postgres

WRONG = {"full_name": "Aaliyah Alvarez", "date_of_birth": "1958-02-01", "zip": "11201"}


async def log(s: CallSession, answered_by: str = "none") -> dict[str, Any]:
    logged = await s.dispatch("log_contact_attempt", {"answered_by": answered_by, "summary": "Call closed."})
    assert "error" not in logged, logged
    return logged


def remi_attempt(dsn: str) -> dict[str, Any]:
    (attempt,) = rows(dsn, "contact_attempts", "agent_name = 'Remi'")
    return attempt


# --- refusal to verify -----------------------------------------------------------------------


async def test_refusal_is_its_own_disposition_and_writes_nothing_else(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    refused = await s.dispatch("record_verification_refused", {})
    assert refused["recorded"] == "refused_verification"
    assert "+12125550100" in refused["next"]  # the agency row's callback number, answered by a person
    assert "error" in await s.dispatch("get_debt_details", DISCLOSED)
    assert (await log(s, "third_party"))["disposition"] == "refused_verification"
    attempt = remi_attempt(pg_dsn)
    assert (attempt["answered_by"], attempt["consumer_responded"]) == (None, False)
    assert rows(pg_dsn, "account_flags") == [] and rows(pg_dsn, "letters") == []
    assert rows(pg_dsn, "phones")[0]["consent_revoked_at"] is None


async def test_wrong_answers_are_a_failure_even_if_they_then_refuse(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    assert (await s.dispatch("verify_right_party", WRONG))["verified"] is False
    await s.dispatch("record_verification_refused", {})
    assert (await log(s))["disposition"] == "failed_verification"


async def test_a_refusal_hung_up_on_is_still_logged_as_a_refusal(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("record_verification_refused", {})
    await s.finalize()
    assert (remi_attempt(pg_dsn)["answered_by"], remi_attempt(pg_dsn)["disposition"]) == (
        None,
        "refused_verification",
    )


async def test_a_person_who_refused_may_still_verify(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("record_verification_refused", {})
    assert (await s.dispatch("verify_right_party", VERIFY))["verified"] is True
    assert "error" in await s.dispatch("record_verification_refused", {})
    assert (await log(s, "consumer"))["disposition"] == "no_commitment"


# --- stop requests -----------------------------------------------------------------------------


async def test_unverified_stop_stops_the_number_not_the_account(pg_dsn, make_session, ticks) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    cease = await s.dispatch("record_flag", {"kind": "cease"})
    assert "stop_calling_this_number" in cease["error"]
    ticks.t += 20
    stopped = await s.dispatch("stop_calling_this_number", {})
    assert stopped["recorded"] == "unverified_stop_request"
    assert "error" in await s.dispatch("verify_right_party", VERIFY)  # no verification afterwards
    assert (await log(s, "third_party"))["disposition"] == "unverified_stop"

    assert remi_attempt(pg_dsn)["answered_by"] is None
    (flag,) = rows(pg_dsn, "account_flags")
    assert (flag["kind"], flag["details"]) == ("unverified_stop_request", {"phone_id": "ph_420101_1"})
    phone = rows(pg_dsn, "phones")[0]
    assert phone["consent_revoked_at"] == datetime.fromisoformat("2027-02-10T09:00:20-05:00")
    assert phone["status"] == "valid"
    assert rows(pg_dsn, "accounts")[0]["status"] == "active"
    assert rows(pg_dsn, "letters") == []


async def test_wrong_number_stop_revokes_consent_without_a_flag(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("stop_calling_this_number", {"wrong_number": True})
    await s.finalize()  # the person hung up before Remi logged
    attempt = remi_attempt(pg_dsn)
    assert (attempt["answered_by"], attempt["disposition"]) == ("wrong_number", "wrong_number")
    phone = rows(pg_dsn, "phones")[0]
    assert phone["status"] == "wrong_number" and phone["consent_revoked_at"] is not None
    assert rows(pg_dsn, "account_flags") == []


async def test_a_verified_stop_is_a_cease(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await verified(s)
    assert "record_flag 'cease'" in (await s.dispatch("stop_calling_this_number", {}))["error"]
    assert (await s.dispatch("record_flag", {"kind": "cease"}))["recorded"] == "cease"


async def test_insurance_is_a_dispute_reason(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, creditor_kind="medical")
    s = await make_session()
    await verified(s)
    await s.dispatch("get_debt_details", DISCLOSED)
    assert (await s.dispatch("log_dispute", {"reason": "insurance"}))["recorded"] == "dispute"
    (flag,) = rows(pg_dsn, "account_flags")
    assert (flag["kind"], flag["details"]) == ("dispute", {"reason": "insurance"})
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["substantiation_instructions"]


def rendered_prompt() -> str:
    context = CallContext("TAS-420101", "ph_420101_1", datetime.fromisoformat(CALL_TIME))
    agency = Agency("Tallmere Account Services, LLC", "2091843-DCA", "+12125550100", "Dana Whitlock")
    return render_instructions(context, agency)


def test_the_greeting_is_one_short_turn_asking_for_the_consumer_by_full_name() -> None:
    text = rendered_prompt()
    greeting = text.split("\n1. ")[1].split("\n2. ")[0]
    assert "this is Remi with Tallmere Account Services, an\n   automated assistant." in greeting
    assert "May I speak with <the consumer's full name from get_account>?" in greeting
    # The pre-verification answers are not part of the opening.
    assert "Do not say\n   why you are calling, that it is a personal business matter" in greeting


def test_the_prompt_answers_pre_verification_questions_one_at_a_time() -> None:
    text = rendered_prompt()
    section = text.split("# Before verification")[1].split("# Someone other than the consumer")[0]
    for permitted in (
        "never re-introduce yourself",
        "Never recite this list",
        '"Yes, I\'m an automated assistant."',
        "personal business matter I can only discuss with <the consumer's",
        "the number the agency has on file",
        "2 1 2, 5 5 5, 0 1 0 0, answered by a\n  person",
        "Never the debt, the creditor, an amount",
        "record_verification_refused",
        "stop_calling_this_number",
    ):
        assert permitted in section, permitted
    refusal = section.split("- They will not verify")[1].split("- They ask you to stop calling")[0]
    assert "give the callback number 2 1 2, 5 5 5, 0 1 0 0" in refusal
    assert "then say goodbye. Only then log_contact_attempt" in refusal


# --- the bench's cases, scripted, on the whole World ---------------------------------------------

#: The answer keys' postconditions (manifest.json), unchanged.
REMI_128_POSTCONDITIONS = [
    {
        "op": "insert",
        "row": {
            "answered_by": None,
            "consumer_responded": False,
            "direction": "outbound",
            "disposition": "refused_verification",
            "medium": "call",
            "phone_id": "ph_420228_1",
        },
        "table": "contact_attempts",
    }
]
REMI_134_POSTCONDITIONS = [
    {
        "op": "insert",
        "row": {
            "answered_by": None,
            "consumer_responded": False,
            "direction": "outbound",
            "disposition": "unverified_stop",
            "medium": "call",
            "phone_id": "ph_420234_1",
        },
        "table": "contact_attempts",
    },
    {
        "key": "ph_420234_1",
        "op": "update",
        "set": {"consent_revoked_at": "<during this call>"},
        "table": "phones",
    },
    {
        "op": "insert",
        "row": {"details": {"phone_id": "ph_420234_1"}, "kind": "unverified_stop_request"},
        "table": "account_flags",
    },
]


def matches_answer_key(found: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    ignored = IGNORED - {"consent_revoked_at"}  # its presence is the change; its value is the clock's
    strip = lambda c: {  # noqa: E731
        **{k: v for k, v in comparable(c).items() if k not in ("row", "set")},
        **{
            part: {k: v for k, v in comparable(c)[part].items() if k not in ignored and k != "flag_id"}
            for part in ("row", "set")
            if part in c
        },
    }
    key = lambda c: (c["table"], c["op"])  # noqa: E731
    got = sorted(map(strip, found), key=key)
    for change in got:
        if "consent_revoked_at" in change.get("set", {}):
            change["set"]["consent_revoked_at"] = "<during this call>"
    return got == sorted(map(strip, expected), key=key)


async def scripted(make_session, account_id: str, phone_id: str, call_time: str) -> CallSession:
    s = await make_session(CallContext(account_id, phone_id, datetime.fromisoformat(call_time)))
    opened = await open_call(s)
    assert opened["call_assessment"]["proceed"] is True
    return s


async def test_remi_128_refuses_to_verify(pg_dsn, make_session) -> None:
    """'Yes, it's me.' / 'No, I will not. Send me a letter.': no disclosure, the callback number, a refusal."""
    seed_full_world(pg_dsn)
    before = snapshot(pg_dsn)
    s = await scripted(make_session, "TAS-420228", "ph_420228_1", "2027-02-10T17:31:00-05:00")
    refused = await s.dispatch("record_verification_refused", {})
    assert "callback number" in refused["next"]
    await log(s)
    assert (await s.dispatch("end_call", {}))["ended"] is True
    assert matches_answer_key(changes(before, snapshot(pg_dsn)), REMI_128_POSTCONDITIONS)


async def test_remi_134_unverified_stop(pg_dsn, make_session) -> None:
    """'Who's this?' / 'Stop calling me.': the number stops, the account does not."""
    seed_full_world(pg_dsn)
    before = snapshot(pg_dsn)
    s = await scripted(make_session, "TAS-420234", "ph_420234_1", "2027-02-10T14:49:00-05:00")
    assert "error" in await s.dispatch("record_flag", {"kind": "cease"})  # what the smoke run tried
    await s.dispatch("stop_calling_this_number", {})
    await log(s)
    assert (await s.dispatch("end_call", {}))["ended"] is True
    assert matches_answer_key(changes(before, snapshot(pg_dsn)), REMI_134_POSTCONDITIONS)
