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

WRONG = {"name": "Aaliyah Alvarez", "date_of_birth": "1958-02-01", "zip": "11201"}


async def log(s: CallSession, answered_by: str = "none") -> dict[str, Any]:
    """Log the call and hang up: end_call writes the contact row."""
    logged = await s.dispatch("log_contact_attempt", {"answered_by": answered_by, "summary": "Call closed."})
    assert "error" not in logged, logged
    ended = await s.dispatch("end_call", {})
    assert ended["disposition"] == logged["disposition"], ended
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
    assert "+12125550100" in refused["allowed_now"]  # the agency row's callback number, answered by a person
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


async def test_a_plain_wrong_number_marks_the_phone_and_keeps_its_consent(pg_dsn, make_session) -> None:
    """REMI-079: "You got the wrong number" alone marks the phone wrong_number; consent stays."""
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    reported = await s.dispatch("report_wrong_number", {"asked_to_stop_calling": False})
    assert (reported["recorded"], reported["consent_revoked"]) == ("wrong_number", False)
    assert "error" in await s.dispatch("verify_right_party", VERIFY)  # nothing more is asked
    assert (await log(s, "wrong_number"))["disposition"] == "wrong_number"
    attempt = remi_attempt(pg_dsn)
    assert (attempt["answered_by"], attempt["consumer_responded"]) == ("wrong_number", False)
    phone = rows(pg_dsn, "phones")[0]
    assert phone["status"] == "wrong_number" and phone["consent_revoked_at"] is None
    assert rows(pg_dsn, "account_flags") == [] and rows(pg_dsn, "letters") == []


@pytest.mark.parametrize("order", ["reported_with_stop", "reported_then_stop"])
async def test_a_wrong_number_that_says_stop_also_revokes_consent(pg_dsn, make_session, order) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    if order == "reported_with_stop":
        await s.dispatch("report_wrong_number", {"asked_to_stop_calling": True})
    else:
        await s.dispatch("report_wrong_number", {"asked_to_stop_calling": False})
        assert (await s.dispatch("stop_calling_this_number", {}))["recorded"] == "wrong_number_stop"
    await s.finalize()  # the person hung up before Remi logged
    attempt = remi_attempt(pg_dsn)
    assert (attempt["answered_by"], attempt["disposition"]) == ("wrong_number", "wrong_number")
    phone = rows(pg_dsn, "phones")[0]
    assert phone["status"] == "wrong_number" and phone["consent_revoked_at"] is not None
    assert rows(pg_dsn, "account_flags") == []


async def test_the_retired_wrong_number_argument_is_refused_and_writes_nothing(pg_dsn, make_session) -> None:
    """The old call shape, stop_calling_this_number(wrong_number=true), must not run as a plain unverified
    stop (consent revoked and a review flag, the phone never marked wrong_number): it is refused, pointing
    at report_wrong_number."""
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    for args in ({"wrong_number": True}, {"wrong_number": False}):
        refused = await s.dispatch("stop_calling_this_number", args)
        assert "report_wrong_number" in refused["error"] and "nothing was recorded" in refused["error"]
    assert "not permitted" in (await s.dispatch("stop_calling_this_number", {"reason": "asked"}))["error"]
    phone = rows(pg_dsn, "phones")[0]
    assert (phone["status"], phone["consent_revoked_at"]) == ("valid", None)
    assert rows(pg_dsn, "account_flags") == []
    assert s.state.number_stop is None
    assert (await s.dispatch("stop_calling_this_number", {}))["recorded"] == "unverified_stop_request"


async def test_each_phone_change_is_written_once(pg_dsn, make_session, monkeypatch) -> None:
    """The bench grades the SQL log: one consent revocation and one wrong_number mark, however the person
    put it (wrong number and stop, then stop again; the model's log also saying the number is wrong)."""
    import psycopg

    writes: list[str] = []
    execute = psycopg.AsyncConnection.execute

    async def spy(self, query, params=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        text = query.as_string(self) if hasattr(query, "as_string") else str(query)
        if text.startswith('update "phones"'):
            writes.append(text)
        return await execute(self, query, params, *args, **kwargs)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", spy)
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("report_wrong_number", {"asked_to_stop_calling": True})
    await s.dispatch("stop_calling_this_number", {})
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "wrong_number", "summary": "Wrong number.", "number_is_wrong": True},
    )
    await s.dispatch("end_call", {})
    assert sorted("consent_revoked_at" in w for w in writes) == [False, True], writes


async def test_a_wrong_number_logged_without_the_tool_still_marks_the_phone_once(
    pg_dsn, make_session
) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("report_wrong_number", {"asked_to_stop_calling": False})
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "wrong_number", "summary": "Wrong number.", "number_is_wrong": True},
    )
    await s.dispatch("end_call", {})
    assert rows(pg_dsn, "phones")[0]["status"] == "wrong_number"

    seed_world(pg_dsn)
    s2 = await make_session()
    await open_call(s2)
    await log(s2, "wrong_number")  # the model only logged it
    assert rows(pg_dsn, "phones")[0]["status"] == "wrong_number"


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


def section(text: str, heading: str) -> str:
    return text.split(f"# {heading}\n")[1].split("\n# ")[0]


def test_the_greeting_uses_a_given_name_and_never_re_asks_it() -> None:
    stages = section(rendered_prompt(), "The call, stage by stage")
    greet = stages.split("1. **Greet.**")[1].split("2. **Timing.**")[0]
    assert "If they said their name, use it and\n   never ask for it again" in greet
    assert (
        "an automated assistant calling from\n   Tallmere Account Services, about a personal business matter."
        in greet
    )
    assert "Is this <first name>?" in greet  # only when they gave no name
    assert "full name" not in stages.split("6. **After verification**")[0]


def test_the_stages_explain_the_check_and_verify_one_fact_at_a_time() -> None:
    stages = section(rendered_prompt(), "The call, stage by stage")
    for line in (
        'never "regarding your\naccount"',
        "Is now an okay time?",
        "for their privacy",
        "one at\n   a time; they choose",
        "a first name is enough",
        "Three wrong answers end the call",
        "by their second push-back at\n   the latest",
        "2 1 2, 5 5 5, 0 1 0 0, where a person answers",
        "verify_right_party's result lists the disclosures due",
        "before anything about the account",
        "one per turn",
        "Keep the exact wording",
        "itemization only if asked",
    ):
        assert line in stages, line


def test_push_back_gets_a_reason_and_a_way_out() -> None:
    text = rendered_prompt()
    pushback = section(text, "Push-back before verification")
    for line in (
        '"Why do you need my details?": so their private business isn\'t shared with the wrong person',
        '"Is this a scam?"',
        '"Are you a robot?": yes, an automated assistant',
        "the number the agency has on file",
        "record_verification_refused",
        "stop_calling_this_number",
    ):
        assert line in pushback, line
    refusal = pushback.split("- They won't verify")[1].split('- "Stop calling"')[0]
    assert "give the callback number" in refusal and "never argue" in refusal


def test_no_leaked_example_amount_or_name_and_no_narration() -> None:
    text = rendered_prompt()
    for leaked in ("185", "hundred", "Marisol", "May I speak with"):
        assert leaked not in text, leaked
    talk = section(text, "How you talk")
    assert "No narration" in talk and "Never ask again for something they answered" in talk


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
    assert "+12125550100" in refused["allowed_now"] and refused["callback_number"] == "+12125550100"
    await log(s)  # log, then end_call (which writes the row)
    assert matches_answer_key(changes(before, snapshot(pg_dsn)), REMI_128_POSTCONDITIONS)


async def test_remi_134_unverified_stop(pg_dsn, make_session) -> None:
    """'Who's this?' / 'Stop calling me.': the number stops, the account does not."""
    seed_full_world(pg_dsn)
    before = snapshot(pg_dsn)
    s = await scripted(make_session, "TAS-420234", "ph_420234_1", "2027-02-10T14:49:00-05:00")
    assert "error" in await s.dispatch("record_flag", {"kind": "cease"})  # what the smoke run tried
    await s.dispatch("stop_calling_this_number", {})
    await log(s)  # log, then end_call (which writes the row)
    assert matches_answer_key(changes(before, snapshot(pg_dsn)), REMI_134_POSTCONDITIONS)
