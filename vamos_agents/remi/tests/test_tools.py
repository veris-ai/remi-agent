"""The tools and the session gate against a real Postgres seeded with WORLD.md's schema.

Each flow asserts the exact rows the call leaves, in the shape the benchmark's
answer key (``policy.simulate`` in utility-bench) expects for that case. Cards
and bank accounts go through the World's real payment gateway functions, and
Remi connects as a login that cannot write ``payment_methods`` or ``payments``
itself (see ``support``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pytest

from tests.guards import assert_no_wall_clock
from vamos_voice.session import CallSession

from .support import (
    CALL_TIME,
    DECLINED_CARD,
    DECLINING_BANK,
    EXPIRED_CARD,
    GOOD_BANK,
    GOOD_CARD,
    INSUFFICIENT_FUNDS_CARD,
    all_text,
    changes,
    rows,
    seed_full_world,
    seed_world,
    snapshot,
)

pytestmark = pytest.mark.postgres

DISCLOSED = {
    "recording_disclosure_given": True,
    "mini_miranda_given": True,
    "alternative_format_statement_given": True,
}
VERIFY = {"name": "Aaliyah Alvarez", "date_of_birth": "1958-01-01", "zip": "11215"}


async def open_call(s: CallSession) -> dict:
    for name in ("get_account", "get_dialed_phone"):
        assert "error" not in await s.dispatch(name, {})
    return await s.dispatch("get_contact_history", {})


async def verified(s: CallSession) -> None:
    await open_call(s)
    assert (await s.dispatch("verify_right_party", VERIFY))["verified"] is True


async def unlocked(s: CallSession) -> dict:
    await verified(s)
    details = await s.dispatch("get_debt_details", DISCLOSED)
    assert "error" not in details, details
    return details


async def card(s: CallSession, number: str = GOOD_CARD) -> str:
    pm = await s.dispatch(
        "add_payment_method",
        {"method": "card", "card_number": number, "exp_month": 12, "exp_year": 30, "cvc": "123"},
    )
    assert "error" not in pm, pm
    return pm["method_id"]


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
    assert "verify_right_party" in (await s.dispatch("get_debt_details", DISCLOSED))["error"]
    assert (
        "verify_right_party"
        in (await s.dispatch("take_payment", {"method_id": "x", "amount": 0.01, "purpose": "pay_in_full"}))[
            "error"
        ]
    )
    await s.dispatch("verify_right_party", VERIFY)
    refused = await s.dispatch(
        "get_debt_details", {"recording_disclosure_given": True, "mini_miranda_given": True}
    )
    assert "alternative-format" in refused["error"] and "language" not in refused["error"]
    assert (
        "disclosures"
        in (await s.dispatch("take_payment", {"method_id": "x", "amount": 0.01, "purpose": "pay_in_full"}))[
            "error"
        ]
    )


async def test_english_only_no_language_step(pg_dsn, make_session) -> None:
    """No language tool exists, the opening never asks for one, and the debt unlocks without it."""
    from vamos_agents.remi.tools import REGISTRY

    assert not [t for t in REGISTRY.specs() if "language" in t.name]
    seed_world(pg_dsn)
    s = await make_session()
    assessment = (await open_call(s))["call_assessment"]
    assert not any("language" in k for k in assessment)
    verified_result = await s.dispatch("verify_right_party", VERIFY)
    assert "language" not in str(verified_result)
    before = snapshot(pg_dsn)
    assert "error" not in await s.dispatch("get_debt_details", DISCLOSED)
    assert changes(before, snapshot(pg_dsn)) == []


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


async def test_the_screen_pop_runs_the_connect_reads_and_shows_no_secrets(pg_dsn, make_session) -> None:
    """The dialer's screen pop: the same three handlers on the call's state, as Remi's own SQL, rendered
    without the verification secrets; the gates are past the reads and verification is next."""
    from vamos_agents.remi.screen_pop import HEADING, screen_pop

    seed_world(pg_dsn)
    s = await make_session()
    pop = await screen_pop(s.state)
    text = pop.render()
    assert pop.heading == HEADING == "Account on screen"
    for secret in ("1958", "11215", "2234", "7th Ave", "18500", "Ashcombe", "4001"):
        assert secret not in text
    assert "Call assessment: the call may proceed." in text and '"call_assessment"' in text
    assert "Also call" not in text, "the per-read 'next' hints are stale once all three ran"
    assert s.state.missing_reads == [] and s.tool_log == []
    assert "error" not in await s.dispatch("verify_right_party", VERIFY)


async def test_the_screen_pop_shows_a_call_that_should_not_have_been_placed(pg_dsn, make_session) -> None:
    from vamos_agents.remi.screen_pop import screen_pop

    seed_world(pg_dsn, phone={"source": "employer"})
    s = await make_session()
    text = (await screen_pop(s.state)).render()
    assert "should NOT have been placed (employer_phone)" in text
    assert "should not have been placed" in (await s.dispatch("verify_right_party", VERIFY))["error"]


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


async def test_the_contact_summary_leads_with_what_the_call_recorded(pg_dsn, make_session) -> None:
    """REMI-103: on a should-not-call account the model said only "Hi. I'll keep this brief and then
    I'll let you go." and logged that it had identified itself as Tallmere's virtual assistant. The
    stored summary's facts come from the session, and the model's words are labelled as its notes."""
    seed_world(pg_dsn, phone={"source": "employer"})
    s = await make_session()
    await open_call(s)
    claim = "Remi identified as a virtual assistant for Tallmere Account Services and ended the call."
    await s.dispatch("log_contact_attempt", {"answered_by": "none", "summary": claim})
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert attempt["summary"] == (
        "Disposition: should_not_call. The call should not have been placed (employer_phone): "
        f"no one was verified and the debt was not read. Remi's notes: {claim}"
    )

    seed_world(pg_dsn)
    s2 = await make_session()
    await unlocked(s2)
    await s2.dispatch("log_contact_attempt", {"answered_by": "consumer", "summary": "Reviewed the balance."})
    (attempt,) = rows(pg_dsn, "contact_attempts", "agent_name = 'Remi'")
    assert attempt["summary"].startswith(
        "Disposition: no_commitment. Answered by: the verified consumer. Debt read after attesting: "
        "the recording disclosure, the mini-Miranda, the alternative-format statement."
    )


def test_the_summary_is_asked_for_only_what_happened() -> None:
    from importlib.resources import files

    from vamos_agents.remi.tools.catalog import REGISTRY

    prompt = files("vamos_agents.remi.prompt").joinpath("instructions.md").read_text(encoding="utf-8")
    assert "what actually happened and the outcome; never what you meant or were told to say" in prompt
    (spec,) = [t for t in REGISTRY.specs() if t.name == "log_contact_attempt"]
    described = spec.parameters["properties"]["summary"]["description"]
    assert "never what you meant or were told to say" in described


async def test_three_failed_verifications_lock_the_call(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    wrong = {"name": "Aaliyah Alvarez", "date_of_birth": "1958-02-01", "zip": "11201"}
    assert "not counted" in (await s.dispatch("verify_right_party", {}))["error"]
    results = [await s.dispatch("verify_right_party", wrong) for _ in range(3)]
    assert [r["attempts_left"] for r in results] == [2, 1, 0]
    assert "three" in (await s.dispatch("verify_right_party", VERIFY))["error"]
    logged = await s.dispatch(
        "log_contact_attempt", {"answered_by": "none", "summary": "Caller could not verify identity."}
    )
    assert logged["disposition"] == "failed_verification"


async def test_verification_takes_a_first_name_and_facts_one_at_a_time(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    first = await s.dispatch("verify_right_party", {"name": "Aaliyah", "date_of_birth": "1958-01-01"})
    assert (first["verified"], first["name_confirmed"], first["facts_confirmed"]) == (False, True, 1)
    assert first["attempts_left"] == 3 and "debt" in first["allowed_now"]
    assert "error" in await s.dispatch("get_debt_details", DISCLOSED)
    done = await s.dispatch("verify_right_party", {"name": "Aaliyah", "zip": "11215"})
    assert done["verified"] is True
    assert done["owed_before_the_debt"] == [
        "the recording disclosure",
        "the mini-Miranda",
        "the alternative-format statement with the callback number",
    ]
    assert "next" not in done and "Now give" not in str(done)


async def test_verification_keeps_matched_facts_across_a_wrong_answer(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    await s.dispatch("verify_right_party", {"name": "Aaliyah", "zip": "11215"})
    wrong = await s.dispatch("verify_right_party", {"name": "Aaliyah", "date_of_birth": "1958-02-01"})
    assert (wrong["verified"], wrong["attempts_left"], wrong["facts_confirmed"]) == (False, 2, 1)
    assert "1958" not in str(wrong) and "date" not in str(wrong), "never which fact was wrong"
    assert (await s.dispatch("verify_right_party", {"ssn_last4": "2234"}))["verified"] is True


async def test_facts_without_a_name_wait_for_the_name(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    facts = await s.dispatch("verify_right_party", {"date_of_birth": "1958-01-01", "zip": "11215"})
    assert (facts["verified"], facts["name_confirmed"], facts["facts_confirmed"]) == (False, False, 2)
    assert any("name" in still for still in facts["still_needed"])
    assert (await s.dispatch("verify_right_party", {"name": "aaliyah"}))["verified"] is True


@pytest.mark.parametrize("name", ["Aaliyah Smith", "Alicia", "Aaliyah's sister"])
async def test_a_name_that_is_not_the_consumers_is_a_wrong_answer(pg_dsn, make_session, name) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    result = await s.dispatch("verify_right_party", {**VERIFY, "name": name})
    assert (result["verified"], result["attempts_left"]) == (False, 2)


def test_tool_hints_state_rules_not_lines_to_say() -> None:
    import ast
    from importlib.resources import files

    sources = [files("vamos_agents.remi").joinpath(p) for p in ("call.py", "gates.py")]
    sources += [f for f in files("vamos_agents.remi.tools").iterdir() if f.name.endswith(".py")]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                for script in (
                    "Now give",
                    "Say ",
                    "Tell the",
                    "Explain ",
                    "Acknowledge ",
                    "substantiation of",
                ):
                    assert script not in node.value, (source.name, node.value)


# --- money -----------------------------------------------------------------------------------


def no_cents(value: Any) -> bool:
    if isinstance(value, dict):
        return all("cents" not in k and no_cents(v) for k, v in value.items())
    if isinstance(value, list):
        return all(map(no_cents, value))
    return True


async def test_money_crosses_the_tools_in_dollars(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    details = await unlocked(s)
    assert no_cents(details)
    assert details["current_balance"] == "$185.00"
    assert details["itemization"]["charge_off_balance"].startswith("$")
    assert details["limits"]["hardship_min_instalment"] == "$10.00"
    fraction = await s.dispatch(
        "record_promise", {"amount": 10.005, "due_on": "2027-02-19", "method": "check"}
    )
    assert "dollars and cents" in fraction["error"]
    promise = await s.dispatch("record_promise", {"amount": 50.25, "due_on": "2027-02-19", "method": "check"})
    assert promise["amount"] == "$50.25" and no_cents(promise)
    (row,) = rows(pg_dsn, "promises_to_pay")
    assert row["amount_cents"] == 5025, "the World still stores cents"


#: REMI-001 (pay a small balance in full by card): the answer key's postconditions from
#: utility-bench collections_bench/generated/manifest.json at the commit collections_world.sql names,
#: minus the consumers.language_preference update: the bench is English-only for now, so Remi has no
#: language step and never writes that column.
REMI_001_POSTCONDITIONS = [
    {
        "op": "insert",
        "row": {
            "answered_by": "consumer",
            "consumer_responded": True,
            "direction": "outbound",
            "disposition": "paid",
            "medium": "call",
            "phone_id": "ph_420101_1",
        },
        "table": "contact_attempts",
    },
    {
        "op": "insert",
        "row": {
            "brand": "visa",
            "exp_month": 12,
            "exp_year": 2030,
            "holder_name": "<the account holder's name, as the consumer gave it>",
            "kind": "card",
            "last4": "4242",
        },
        "table": "payment_methods",
    },
    {
        "op": "insert",
        "row": {
            "amount_cents": 18500,
            "decline_code": None,
            "method_id": "<the method_id of the Visa card ending 4242 tokenized on this call>",
            "processor_ref": "<the processor_ref gateway_charge returned>",
            "status": "approved",
        },
        "table": "payments",
    },
    {
        "key": "TAS-420101",
        "op": "update",
        "set": {
            "current_balance_cents": 0,
            "last_payment_on": "2027-02-10",
            "paid_since_charge_off_cents": 18500,
            "status": "paid",
        },
        "table": "accounts",
    },
]
#: What the answer key ignores (WORLD.md): generated ids, timestamps, agent_name; plus the
#: contact row's duration_s and summary, which the assertion checks in words, not by value, and
#: account_id, which every row of the case shares, and holder_name, a placeholder there (checked below).
IGNORED = {
    "account_id",
    "holder_name",
    "attempt_id",
    "payment_id",
    "method_id",
    "processor_ref",
    "created_at",
    "queued_at",
    "at",
    "consent_revoked_at",
    "agent_name",
    "duration_s",
    "summary",
}


def comparable(change: dict[str, Any]) -> dict[str, Any]:
    """A change as the answer key states it: ignored columns dropped and dates as ISO strings."""
    out = {k: v for k, v in change.items() if k not in ("row", "set")}
    for part in ("row", "set"):
        if part in change:
            out[part] = {
                c: v.isoformat() if isinstance(v, date) else v
                for c, v in change[part].items()
                if c not in IGNORED
            }
    return out


async def test_remi_001_pay_in_full_leaves_exactly_the_answer_key_rows(
    pg_dsn, make_session, ticks, caplog
) -> None:
    """REMI-001 on the whole vendored World: the call changes exactly the answer key's rows."""
    seed_full_world(pg_dsn)
    before = snapshot(pg_dsn)
    s = await make_session()
    caplog.set_level(logging.INFO)
    details = await unlocked(s)
    assert details["current_balance"] == "$185.00"
    method_id = await card(s)
    ticks.t += 60
    paid = await s.dispatch(
        "take_payment", {"method_id": method_id, "amount": 185.00, "purpose": "pay_in_full"}
    )
    assert paid == {
        "status": "approved",
        "amount": "$185.00",
        "remaining_balance": "$0.00",
        "paid_with": "card ending 4242",
    }
    ticks.t += 65
    await s.dispatch(
        "log_contact_attempt",
        {"answered_by": "consumer", "summary": "Consumer paid the full balance by card."},
    )

    everything = changes(before, snapshot(pg_dsn))
    # The processor's vault is not the agency's schema: the answer key never names it.
    (vaulted,) = [c for c in everything if c["table"] == "processor_vault"]
    found = [c for c in everything if c["table"] != "processor_vault"]
    key = lambda c: (c["table"], c["op"])  # noqa: E731
    assert sorted(map(comparable, found), key=key) == sorted(
        map(comparable, REMI_001_POSTCONDITIONS), key=key
    )
    by_table = {c["table"]: c for c in found}
    method, payment = by_table["payment_methods"]["row"], by_table["payments"]["row"]
    assert (
        method["holder_name"] == "Aaliyah Alvarez"
        and payment["method_id"] == method["method_id"] == method_id
    )
    assert payment["processor_ref"].startswith("rp_") and payment["account_id"] == "TAS-420101"
    assert vaulted["row"]["method_id"] == method_id
    attempt = by_table["contact_attempts"]["row"]
    assert attempt["duration_s"] == 125 and attempt["at"] == datetime.fromisoformat(CALL_TIME)
    assert payment["at"] == datetime.fromisoformat(
        "2027-02-10T09:01:00-05:00"
    )  # the call's clock, not wall time
    assert GOOD_CARD not in all_text(pg_dsn)
    assert GOOD_CARD not in caplog.text and "[redacted]" in caplog.text


@pytest.mark.parametrize(
    ("number", "decline_code", "reason"),
    [
        (INSUFFICIENT_FUNDS_CARD, "insufficient_funds", "not enough funds"),
        (DECLINED_CARD, "card_declined", "issuer declined"),
        (EXPIRED_CARD, "expired_card", "expired"),
    ],
)
async def test_card_decline_tells_the_model_why_then_a_promise(
    pg_dsn, make_session, number, decline_code, reason
) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    method_id = await card(s, number)
    declined = await s.dispatch(
        "take_payment", {"method_id": method_id, "amount": 185.00, "purpose": "pay_in_full"}
    )
    assert (declined["status"], declined["decline_code"]) == ("declined", decline_code)
    assert reason in declined["reason"] and "did not go through" in declined["allowed_now"]
    assert "promise" in declined["allowed_now"] and "bank account" in declined["allowed_now"]
    again = await s.dispatch(
        "take_payment", {"method_id": method_id, "amount": 185.00, "purpose": "pay_in_full"}
    )
    assert "already declined" in again["error"]
    assert (await s.dispatch("record_promise", {"amount": 185.00, "due_on": "2027-02-25", "method": "card"}))[
        "error"
    ]
    assert (await s.dispatch("record_promise", {"amount": 185.00, "due_on": "2027-02-19", "method": "card"}))[
        "recorded"
    ]
    assert (
        await s.dispatch(
            "log_contact_attempt",
            {"answered_by": "consumer", "summary": "Card declined; consumer promised to pay."},
        )
    )["disposition"] == "ptp"
    assert [(p["status"], p["decline_code"]) for p in rows(pg_dsn, "payments")] == [
        ("declined", decline_code)
    ]
    account = rows(pg_dsn, "accounts")[0]
    assert (account["status"], account["current_balance_cents"], account["last_payment_on"]) == (
        "active",
        18500,
        None,
    )


async def test_card_decline_then_ach_pays_in_full(pg_dsn, make_session) -> None:
    """REMI-009's shape: the card declines, the checking account pays."""
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    declined = await s.dispatch(
        "take_payment",
        {
            "method_id": await card(s, INSUFFICIENT_FUNDS_CARD),
            "amount": 185.00,
            "purpose": "pay_in_full",
        },
    )
    assert declined["status"] == "declined"
    bank = await s.dispatch(
        "add_payment_method",
        {"method": "ach", "routing_number": GOOD_BANK[0], "account_number": GOOD_BANK[1]},
    )
    assert (bank["method"], bank["last4"], bank["brand"]) == ("ach", "6789", None)
    paid = await s.dispatch(
        "take_payment", {"method_id": bank["method_id"], "amount": 185.00, "purpose": "pay_in_full"}
    )
    assert paid["status"] == "approved" and paid["paid_with"] == "bank account ending 6789"
    assert (
        await s.dispatch(
            "log_contact_attempt", {"answered_by": "consumer", "summary": "Card declined; paid by ACH."}
        )
    )["disposition"] == "paid"
    assert [
        (m["kind"], m["last4"], m["brand"])
        for m in rows(pg_dsn, "payment_methods", "true order by kind desc")
    ] == [("card", "9995", "visa"), ("ach", "6789", None)]
    assert sorted((p["status"], p["amount_cents"]) for p in rows(pg_dsn, "payments")) == [
        ("approved", 18500),
        ("declined", 18500),
    ]
    account = rows(pg_dsn, "accounts")[0]
    assert (account["status"], account["current_balance_cents"], account["paid_since_charge_off_cents"]) == (
        "paid",
        0,
        18500,
    )


async def test_ach_decline_is_reported(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    bank = await s.dispatch(
        "add_payment_method",
        {"method": "ach", "routing_number": DECLINING_BANK[0], "account_number": DECLINING_BANK[1]},
    )
    declined = await s.dispatch(
        "take_payment", {"method_id": bank["method_id"], "amount": 185.00, "purpose": "pay_in_full"}
    )
    assert (declined["status"], declined["decline_code"], declined["declined"]) == (
        "declined",
        "insufficient_funds",
        "bank account ending 2227",
    )
    assert rows(pg_dsn, "accounts")[0]["status"] == "active"


async def test_gateway_validation_errors_reach_the_model_and_write_nothing(
    pg_dsn, make_session, caplog
) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await unlocked(s)
    caplog.set_level(logging.INFO)
    bad_luhn = "4242424242424241"
    refused = await s.dispatch(
        "add_payment_method",
        {"method": "card", "card_number": bad_luhn, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    )
    assert refused == {
        "error": "The payment gateway refused it: the card number is not valid (it fails the Luhn check)"
    }
    expired = await s.dispatch(
        "add_payment_method",
        {"method": "card", "card_number": GOOD_CARD, "exp_month": 1, "exp_year": 27, "cvc": "123"},
    )
    assert "the card expired at the end of 01/2027" in expired["error"]
    missing = await s.dispatch("add_payment_method", {"method": "card", "card_number": GOOD_CARD})
    assert "expiry" in missing["error"]
    over = await s.dispatch(
        "take_payment", {"method_id": await card(s), "amount": 185.01, "purpose": "settlement"}
    )
    assert "full balance" in over["error"]  # refused by policy before the gateway is reached
    assert [m["last4"] for m in rows(pg_dsn, "payment_methods")] == ["4242"]
    assert rows(pg_dsn, "payments") == []
    assert bad_luhn not in caplog.text and GOOD_CARD not in caplog.text


async def test_settlement_below_the_floor_goes_to_a_person(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, account={"current_balance_cents": 200000, "charge_off_balance_cents": 200000})
    s = await make_session()
    await unlocked(s)
    pm = await card(s)
    refused = await s.dispatch("take_payment", {"method_id": pm, "amount": 800.00, "purpose": "settlement"})
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
    method_id = await card(s)
    ok = await s.dispatch("take_payment", {"method_id": method_id, "amount": 750.00, "purpose": "settlement"})
    assert ok["status"] == "approved" and ok["remaining_balance"] == "$250.00"
    (arr,) = rows(pg_dsn, "arrangements")
    assert (arr["kind"], arr["total_cents"], arr["instalments"], arr["method_id"]) == (
        "settlement_lump",
        75000,
        [{"due_on": "2027-02-10", "amount_cents": 75000}],
        method_id,
    )
    assert arr["confirmation_letter_due_by"] == date(2027, 2, 18)
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["plan_confirmation"]
    (payment,) = rows(pg_dsn, "payments")
    assert (payment["status"], payment["amount_cents"], payment["method_id"]) == (
        "approved",
        75000,
        method_id,
    )
    account = rows(pg_dsn, "accounts")[0]
    assert (account["status"], account["current_balance_cents"], account["paid_since_charge_off_cents"]) == (
        "settled",
        25000,
        75000,
    )


async def test_hardship_plan_needs_the_hardship_recorded_first(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, account={"current_balance_cents": 96000, "charge_off_balance_cents": 96000})
    s = await make_session()
    await unlocked(s)
    pm = await card(s)
    plan = {
        "kind": "hardship_plan",
        "method_id": pm,
        "instalment_count": 24,
        "first_due_on": "2027-03-01",
    }
    assert "hardship recorded first" in (await s.dispatch("create_arrangement", plan))["error"]
    await s.dispatch("record_flag", {"kind": "hardship", "hardship_reason": "job_loss"})
    made = await s.dispatch("create_arrangement", plan)
    assert len(made["instalments"]) == 24 and made["instalments"][0] == {
        "due_on": "2027-03-01",
        "amount": "$40.00",
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
    assert (await s.dispatch("verify_right_party", {"name": "Simone Achterberg", "role": "estate_rep"}))[
        "verified"
    ]
    details = await s.dispatch(
        "get_debt_details", {"recording_disclosure_given": True, "mini_miranda_given": True}
    )
    assert "limits" not in details and details["current_balance"] == "$185.00"
    assert (
        "Collection stopped"
        in (await s.dispatch("record_promise", {"amount": 1.00, "due_on": "2027-02-12", "method": "check"}))[
            "error"
        ]
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
    from vamos_agents.remi import agent, call, context, db, gates, payments, policy, prompt
    from vamos_agents.remi.tools import connect, flags, identity, money, wrapup

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
