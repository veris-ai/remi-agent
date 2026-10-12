"""The creditor's menu: Remi offers, charges and promises only what the World's decision engine approves.

Against the real Postgres with the World's own decision engine (``creditor_policy.sql``, vendored):
the ladder as the model sees it, menu-only enforcement in code (off-menu ids, settlements before a
hardship, approval tiers without or after a denied approval), the World's own refusal surfaced,
plans on the ledger's schedule, the transfer and one-charge guards, and the opening that carries
the disclosures.
"""

from __future__ import annotations

import asyncio
from datetime import date
from importlib.resources import files
from typing import Any

import pytest

from vamos_agents.remi.ledger import LedgerUnavailable
from vamos_agents.remi.tools import REGISTRY
from vamos_voice.session import CallSession

from . import support
from .support import DECLINED_CARD, rows, seed_world
from .test_tools import DISCLOSED, VERIFY, card, open_call, unlocked

pytestmark = pytest.mark.postgres

BALANCE_1000 = {"current_balance_cents": 100000}
HARDSHIP = {"kind": "hardship", "hardship_reason": "job_loss"}


async def _call(
    pg_dsn: str, make_session: Any, *, policy: dict[str, Any] | None = None, **account: Any
) -> CallSession:
    """A verified, unlocked call on a $1,000.00 balance under ``support.TEST_POLICY`` (with ``policy``'s
    fields), priced by the World's own decision engine."""
    seed_world(pg_dsn, account={**BALANCE_1000, **account}, policy=policy)
    s = await make_session()
    await unlocked(s)
    return s


async def _hardship(s: CallSession) -> None:
    assert (await s.dispatch("record_flag", HARDSHIP))["recorded"] == "hardship"


# --- the opening: nothing before verification, then the disclosures ----------------------------


async def test_the_options_are_locked_until_verification_and_disclosures(pg_dsn, make_session) -> None:
    seed_world(pg_dsn)
    s = await make_session()
    await open_call(s)
    for tool, args in (("get_payment_options", {}), ("request_approval", {"option_id": "settle_30"})):
        assert "Not verified yet" in (await s.dispatch(tool, args))["error"]
    await s.dispatch("verify_right_party", VERIFY)
    for tool, args in (("get_payment_options", {}), ("request_approval", {"option_id": "settle_30"})):
        assert "disclosures" in (await s.dispatch(tool, args))["error"]
    await s.dispatch("get_debt_details", DISCLOSED)
    assert "options" in await s.dispatch("get_payment_options", {})
    assert rows(pg_dsn, "approval_requests") == []


async def test_verification_hands_over_the_disclosures_with_their_wording(pg_dsn, make_session) -> None:
    seed_world(
        pg_dsn, account={"sol_expires_on": date(2026, 1, 1), "time_barred_notice_mailed_on": date(2027, 1, 2)}
    )
    s = await make_session()
    await open_call(s)
    done = await s.dispatch("verify_right_party", VERIFY)
    due = {d["disclosure"]: d for d in done["disclosures_due"]}
    assert list(due) == ["recording", "mini_miranda", "time_barred", "alternative_format"]
    assert due["recording"]["wording"] == (
        "This call is being recorded, and the recording may be used in connection with collecting a debt."
    )
    assert due["mini_miranda"]["wording"] == (
        "This is Tallmere Account Services, a debt collector. This is an attempt to collect a debt, and any "
        "information obtained will be used for that purpose."
    )
    assert due["recording"]["verbatim"] and due["mini_miranda"]["verbatim"]
    assert "+12125550100" in due["alternative_format"]["wording"]
    assert "before anything about the account" in done["allowed_now"]
    assert "language" not in str(done), "the bench is English-only: no language-preference question"


async def test_an_estate_rep_gets_the_disclosures_but_no_options(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, consumer={"estate_rep_name": "Simone Achterberg", "deceased_on": date(2026, 12, 1)})
    s = await make_session()
    await open_call(s)
    done = await s.dispatch("verify_right_party", {"name": "Simone Achterberg", "role": "estate_rep"})
    assert [d["disclosure"] for d in done["disclosures_due"]] == ["recording", "mini_miranda"]
    await s.dispatch("get_debt_details", {"recording_disclosure_given": True, "mini_miranda_given": True})
    assert "Only the verified consumer" in (await s.dispatch("get_payment_options", {}))["error"]


# --- the ladder as the model sees it ---------------------------------------------------------------


async def test_the_ladder_is_in_order_in_dollars_without_percentages(pg_dsn, make_session) -> None:
    s = await _call(pg_dsn, make_session)
    ladder = await s.dispatch("get_payment_options", {})
    assert [o["option_id"] for o in ladder["options"]] == [
        "pif",
        "plan_3",
        "plan_4",
        "plan_5",
        "plan_6",
        "settle_10",
        "settle_20",
        "settle_30",
    ]
    by_id = {o["option_id"]: o for o in ladder["options"]}
    assert by_id["pif"] == {"option_id": "pif", "kind": "pay_in_full", "amount": "$1,000.00"}
    assert by_id["plan_6"] == {"option_id": "plan_6", "kind": "plan", "months": 6, "monthly": "$166.67"}
    assert by_id["settle_10"]["amount"] == "$900.00" and by_id["settle_10"]["pay_by"] == "2027-02-17"
    assert "hardship recorded first" in by_id["settle_10"]["status"]
    assert "%" not in str(ladder) and "discount_pct" not in str(ladder) and "pct" not in str(ladder)
    assert "transfer_to_human" in ladder["allowed_now"] and "settlement" not in ladder
    await _hardship(s)
    after = {o["option_id"]: o for o in (await s.dispatch("get_payment_options", {}))["options"]}
    assert "status" not in after["settle_10"] and "approval" in after["settle_30"]["status"]


async def test_a_creditor_without_settlements_says_so(pg_dsn, make_session) -> None:
    s = await _call(pg_dsn, make_session, policy={"settlement_allowed": False, "settlement_tiers": []})
    ladder = await s.dispatch("get_payment_options", {})
    assert [o["kind"] for o in ladder["options"]] == ["pay_in_full"] + ["plan"] * 4
    assert "allows no settlement" in ladder["settlement"] and "transfer_to_human" in ladder["settlement"]


async def test_a_menu_out_of_step_with_the_ledger_is_refused_and_logged(pg_dsn, make_session, caplog) -> None:
    s = await _call(pg_dsn, make_session, placed_balance_cents=99000)
    refused = await s.dispatch("get_payment_options", {})
    assert "cannot be read" in refused["error"] and "transfer_to_human" in refused["error"]
    assert "the ledger holds 100000" in caplog.text
    pm = await card(s)
    assert (
        "cannot be read"
        in (await s.dispatch("take_payment", {"method_id": pm, "option_id": "settle_10"}))["error"]
    )
    assert rows(pg_dsn, "payments") == []


# --- menu-only enforcement -------------------------------------------------------------------------


async def test_off_menu_and_locked_options_are_refused_before_anything_is_charged(
    pg_dsn, make_session
) -> None:
    s = await _call(pg_dsn, make_session)
    pm = await card(s)
    for option_id, why in (
        ("settle_50", "not one of the creditor's options"),
        ("plan_24", "not one of the creditor's options"),
        ("settle_10", "hardship recorded first"),
    ):
        refused = await s.dispatch("take_payment", {"method_id": pm, "option_id": option_id})
        assert why in refused["error"], (option_id, refused)
    assert (
        "not one of"
        in (
            await s.dispatch(
                "create_arrangement", {"option_id": "plan_9", "method_id": pm, "first_due_on": "2027-03-01"}
            )
        )["error"]
    )
    await _hardship(s)
    no_approval = await s.dispatch("take_payment", {"method_id": pm, "option_id": "settle_30"})
    assert "request_approval" in no_approval["error"]
    promise = await s.dispatch(
        "record_promise", {"option_id": "settle_30", "due_on": "2027-02-12", "method": "check"}
    )
    assert "request_approval" in promise["error"]
    assert rows(pg_dsn, "payments") == [] and rows(pg_dsn, "promises_to_pay") == []
    assert support.LEDGER.writes == []


async def test_a_denied_approval_is_never_chargeable(pg_dsn, make_session) -> None:
    """The double approves only with a hardship flag on the account; one recorded on a past call counts for
    the World, but Remi needs the hardship heard on this call before it even asks."""
    s = await _call(pg_dsn, make_session, policy={"settlement_requires_hardship": False})
    pm = await card(s)
    denied = await s.dispatch("request_approval", {"option_id": "settle_30"})
    assert (denied["approved"], denied["reason"]) == (
        False,
        "a settlement that needs approval needs a recorded hardship on the account",
    )
    assert "not offered" in denied["allowed_now"]
    again = await s.dispatch("request_approval", {"option_id": "settle_30"})
    assert again["approved"] is False and again["reason"] == "already decided on this call"
    refused = await s.dispatch("take_payment", {"method_id": pm, "option_id": "settle_30"})
    assert "approval was denied" in refused["error"]
    ladder = {o["option_id"]: o for o in (await s.dispatch("get_payment_options", {}))["options"]}
    assert "denied" in ladder["settle_30"]["status"]
    assert [a["approved"] for a in rows(pg_dsn, "approval_requests")] == [False]
    assert rows(pg_dsn, "payments") == []


async def test_approval_needs_the_hardship_first_then_the_tier_can_be_taken(pg_dsn, make_session) -> None:
    s = await _call(pg_dsn, make_session)
    early = await s.dispatch("request_approval", {"option_id": "settle_30"})
    assert "hardship recorded first" in early["error"] and rows(pg_dsn, "approval_requests") == []
    assert (
        "not an option that needs approval"
        in (await s.dispatch("request_approval", {"option_id": "settle_10"}))["error"]
    )
    await _hardship(s)
    approved = await s.dispatch("request_approval", {"option_id": "settle_30"})
    assert approved["approved"] is True and approved["option"]["amount"] == "$700.00"
    paid = await s.dispatch("take_payment", {"method_id": await card(s), "option_id": "settle_30"})
    assert paid["status"] == "approved" and paid["amount"] == "$700.00" and "settlement_record" not in paid
    (arr,) = rows(pg_dsn, "arrangements")
    assert (arr["kind"], arr["total_cents"]) == ("settlement_lump", 70000)


async def test_the_worlds_off_menu_refusal_reaches_the_model_and_the_log(
    pg_dsn, make_session, caplog
) -> None:
    """Remi's own gate is bypassed here (a forged approval), so the World's refusal is what is tested:
    gateway_charge refuses before any payment row, the model hears why, and the call's charge is free."""
    s = await _call(pg_dsn, make_session)
    await _hardship(s)
    s.state.approvals["settle_30"] = True  # never asked of the World
    refused = await s.dispatch("take_payment", {"method_id": await card(s), "option_id": "settle_30"})
    assert "refused it as outside the creditor's policy" in refused["error"]
    assert "needs an approval from request_approval first" in refused["error"]
    assert "the World refused gateway_charge" in caplog.text
    assert rows(pg_dsn, "payments") == [] and rows(pg_dsn, "arrangements") == []
    assert s.state.charged_payment_id is None and s.state.charge_in_flight is False
    assert support.LEDGER.writes == []


async def test_a_creditor_without_settlements_refuses_them(pg_dsn, make_session) -> None:
    s = await _call(pg_dsn, make_session, policy={"settlement_allowed": False})  # tiers listed anyway
    await _hardship(s)
    refused = await s.dispatch("take_payment", {"method_id": await card(s), "option_id": "settle_10"})
    assert "allows no settlement" in refused["error"]


# --- settlements: one charge, the ledger, the transfer ------------------------------------------


async def _settling(pg_dsn: str, make_session: Any) -> CallSession:
    s = await _call(pg_dsn, make_session)
    await _hardship(s)
    return s


SETTLE_20 = {"option_id": "settle_20"}  # $800.00 of $1,000.00, no approval needed


async def test_lump_settlement_writes_payment_arrangement_and_letter(pg_dsn, make_session) -> None:
    s = await _settling(pg_dsn, make_session)
    method_id = await card(s)
    ok = await s.dispatch("take_payment", {"method_id": method_id, **SETTLE_20})
    assert ok["status"] == "approved" and ok["remaining_balance"] == "$200.00"
    (arr,) = rows(pg_dsn, "arrangements")
    assert (arr["kind"], arr["total_cents"], arr["instalments"], arr["method_id"]) == (
        "settlement_lump",
        80000,
        [{"due_on": "2027-02-10", "amount_cents": 80000}],
        method_id,
    )
    assert arr["confirmation_letter_due_by"] == date(2027, 2, 18)
    assert [letter["kind"] for letter in rows(pg_dsn, "letters")] == ["plan_confirmation"]
    (payment,) = rows(pg_dsn, "payments")
    assert (payment["status"], payment["amount_cents"]) == ("approved", 80000)
    assert [(op, w.get("amount_cents")) for op, w in support.LEDGER.writes] == [
        ("repayment", 80000),
        ("writeoff", 20000),
    ]
    assert support.LEDGER.writes[1][1]["external_id"] == f"wo_{payment['payment_id']}"
    assert (support.LEDGER.balance("TAS-420101"), support.LEDGER.status("TAS-420101")) == (0, 601)
    assert (
        await s.dispatch("log_contact_attempt", {"answered_by": "consumer", "summary": "Settled by card."})
    )["disposition"] == "settled"


async def test_an_approved_settlement_the_ledger_cannot_post_is_never_charged_again(
    pg_dsn, make_session, monkeypatch
) -> None:
    s = await _settling(pg_dsn, make_session)
    pm = await card(s)

    async def unreachable(*_a: Any, **_k: Any) -> Any:
        raise LedgerUnavailable("the ledger could not be reached: connection refused")

    monkeypatch.setattr(support.LEDGER, "post_payment", unreachable)
    paid = await s.dispatch("take_payment", {"method_id": pm, **SETTLE_20})
    assert paid["status"] == "approved" and "posting it to the account's ledger failed" in paid["ledger"]
    for option_id in ("settle_20", "pif"):
        again = await s.dispatch("take_payment", {"method_id": pm, "option_id": option_id})
        assert "already went through" in again["error"]
    assert [p["status"] for p in rows(pg_dsn, "payments")] == ["approved"]


async def test_concurrent_duplicate_settlement_charges_charge_once(pg_dsn, make_session) -> None:
    s = await _settling(pg_dsn, make_session)
    pm = await card(s)
    results = await asyncio.gather(
        s.dispatch("take_payment", {"method_id": pm, **SETTLE_20}),
        s.dispatch("take_payment", {"method_id": pm, **SETTLE_20}),
    )
    assert sorted("error" in r for r in results) == [False, True], results
    assert [p["amount_cents"] for p in rows(pg_dsn, "payments")] == [80000]
    assert [op for op, _ in support.LEDGER.writes] == ["repayment", "writeoff"]


async def test_a_declined_settlement_charge_releases_the_charge_for_a_retry(pg_dsn, make_session) -> None:
    s = await _settling(pg_dsn, make_session)
    declined = await s.dispatch("take_payment", {"method_id": await card(s, DECLINED_CARD), **SETTLE_20})
    assert declined["status"] == "declined"
    assert s.state.charged_payment_id is None and s.state.charge_in_flight is False
    retried = await s.dispatch("take_payment", {"method_id": await card(s), **SETTLE_20})
    assert retried["status"] == "approved"
    assert [p["status"] for p in rows(pg_dsn, "payments")] == ["declined", "approved"]


async def test_a_transfer_is_terminal(pg_dsn, make_session) -> None:
    s = await _settling(pg_dsn, make_session)
    pm = await card(s)
    await s.dispatch("transfer_to_human", {"reason": "outside_creditor_options"})
    for later in (
        ("get_payment_options", {}),
        ("request_approval", {"option_id": "settle_30"}),
        ("take_payment", {"method_id": pm, **SETTLE_20}),
        ("take_payment", {"method_id": pm, "option_id": "pif"}),
        ("create_arrangement", {"option_id": "plan_3", "method_id": pm, "first_due_on": "2027-03-01"}),
        ("record_promise", {"option_id": "pif", "due_on": "2027-02-19", "method": "check"}),
        ("add_payment_method", {"method": "ach", "routing_number": "021000021", "account_number": "1"}),
    ):
        refused = await s.dispatch(*later)
        assert "transferred to a person" in refused["error"], (later, refused)
    assert (
        await s.dispatch(
            "log_contact_attempt", {"answered_by": "consumer", "summary": "Could not pay; transferred."}
        )
    )["disposition"] == "transferred"
    assert rows(pg_dsn, "payments") == [] and rows(pg_dsn, "arrangements") == []


async def test_a_settlement_promise_falls_by_its_pay_by_date(pg_dsn, make_session) -> None:
    s = await _settling(pg_dsn, make_session)
    late = await s.dispatch(
        "record_promise", {"option_id": "settle_20", "due_on": "2027-02-18", "method": "ach"}
    )
    assert "paid by 2027-02-17" in late["error"]
    plan = await s.dispatch(
        "record_promise", {"option_id": "plan_3", "due_on": "2027-02-12", "method": "ach"}
    )
    assert "create_arrangement" in plan["error"]
    ok = await s.dispatch(
        "record_promise", {"option_id": "settle_20", "due_on": "2027-02-17", "method": "ach"}
    )
    assert ok["amount"] == "$800.00"
    assert [r["amount_cents"] for r in rows(pg_dsn, "promises_to_pay")] == [80000]


# --- plans -------------------------------------------------------------------------------------------


async def test_a_plan_re_ages_the_loan_and_records_the_ledgers_schedule(pg_dsn, make_session) -> None:
    seed_world(pg_dsn, account={"current_balance_cents": 26500})
    s = await make_session()
    await unlocked(s)
    method_id = await card(s)
    plan = {"option_id": "plan_6", "method_id": method_id, "first_due_on": "2027-03-01"}
    made = await s.dispatch("create_arrangement", plan)
    assert [i["amount"] for i in made["instalments"]] == ["$44.17"] * 5 + ["$44.15"]
    (arr,) = rows(pg_dsn, "arrangements")
    assert (arr["kind"], arr["total_cents"]) == ("plan", 26500)
    assert [i["amount_cents"] for i in arr["instalments"]] == [4417] * 5 + [4415]
    ((op, write),) = support.LEDGER.writes
    assert (op, write["instalments"], write["first_due_on"], write["external_id"]) == (
        "reAge",
        6,
        date(2027, 3, 1),
        arr["arrangement_id"],
    )
    late = await s.dispatch("create_arrangement", {**plan, "first_due_on": "2027-03-13"})
    assert "on or before 2027-03-12" in late["error"]
    assert (
        await s.dispatch(
            "log_contact_attempt", {"answered_by": "consumer", "summary": "Set up a six-month plan."}
        )
    )["disposition"] == "plan"


async def test_a_plan_whose_ledger_schedule_is_not_the_option_is_taken_back(
    pg_dsn, make_session, caplog, monkeypatch
) -> None:
    from dataclasses import replace

    from vamos_agents.remi.tools import money

    seed_world(pg_dsn, account={"current_balance_cents": 26500})
    priced = money.current_menu

    async def mispriced(session: Any, debt: Any) -> Any:  # plan_6 at $45.00; the ledger spreads $44.17
        menu = await priced(session, debt)
        return replace(
            menu,
            options=tuple(replace(o, monthly_cents=4500) if o.id == "plan_6" else o for o in menu.options),
        )

    monkeypatch.setattr(money, "current_menu", mispriced)
    s = await make_session()
    await unlocked(s)
    refused = await s.dispatch(
        "create_arrangement",
        {"option_id": "plan_6", "method_id": await card(s), "first_due_on": "2027-03-01"},
    )
    assert "not plan plan_6's terms" in refused["error"] and "transfer_to_human" in refused["error"]
    assert [op for op, _ in support.LEDGER.writes] == ["reAge", "undoReAge"]
    assert rows(pg_dsn, "arrangements") == [] and "is not 6 x 4500 cents" in caplog.text


async def test_a_plan_the_agency_cannot_record_is_taken_back_from_the_ledger(
    pg_dsn, make_session, monkeypatch
) -> None:
    from vamos_agents.remi.tools import money

    seed_world(pg_dsn, account={"current_balance_cents": 26500})
    s = await make_session()
    await unlocked(s)
    method_id = await card(s)

    async def refused(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(money, "_insert_arrangement", refused)
    failed = await s.dispatch(
        "create_arrangement", {"option_id": "plan_6", "method_id": method_id, "first_due_on": "2027-03-01"}
    )
    assert "error" in failed
    assert [op for op, _ in support.LEDGER.writes] == ["reAge", "undoReAge"]
    assert rows(pg_dsn, "arrangements") == []


async def test_a_plan_with_a_down_payment_charges_it_then_spreads_the_rest(pg_dsn, make_session) -> None:
    s = await _call(
        pg_dsn,
        make_session,
        policy={"plan_down_pct": 10, "plan_max_months": [{"balance_up_to_cents": None, "months": 3}]},
    )
    pm = await card(s)
    first = await s.dispatch(
        "create_arrangement", {"option_id": "plan_3", "method_id": pm, "first_due_on": "2027-03-01"}
    )
    assert "down payment of $100.00" in first["error"] and support.LEDGER.writes == []
    down = await s.dispatch("take_payment", {"method_id": pm, "option_id": "plan_3"})
    assert (
        down["status"] == "approved" and down["amount"] == "$100.00" and "create_arrangement" in down["next"]
    )
    made = await s.dispatch(
        "create_arrangement", {"option_id": "plan_3", "method_id": pm, "first_due_on": "2027-03-01"}
    )
    assert [i["amount"] for i in made["instalments"]] == ["$300.00"] * 3
    # The World prices the menu from the balance at the start of the call and does not reprice it: after the
    # call's charge it is shown, not refused as out of step with the ledger.
    assert "options" in await s.dispatch("get_payment_options", {})
    assert [op for op, _ in support.LEDGER.writes] == ["repayment", "reAge"]
    assert s.state.outcomes == {"plan"}


async def test_a_down_payment_that_did_not_post_sets_up_no_plan(
    pg_dsn, make_session, monkeypatch, caplog
) -> None:
    s = await _call(
        pg_dsn,
        make_session,
        policy={"plan_down_pct": 10, "plan_max_months": [{"balance_up_to_cents": None, "months": 3}]},
    )
    pm = await card(s)

    async def unreachable(*_a: Any, **_k: Any) -> Any:
        raise LedgerUnavailable("connection refused")

    monkeypatch.setattr(support.LEDGER, "post_payment", unreachable)
    assert (await s.dispatch("take_payment", {"method_id": pm, "option_id": "plan_3"}))["ledger"]
    refused = await s.dispatch(
        "create_arrangement", {"option_id": "plan_3", "method_id": pm, "first_due_on": "2027-03-01"}
    )
    assert "no plan was set up" in refused["error"] and "priced on 90000 cents" in caplog.text
    assert rows(pg_dsn, "arrangements") == []


# --- the prompt and the shared registry ------------------------------------------------------------


def test_the_prompt_runs_the_creditors_ladder_and_routes_every_intent_to_its_tool() -> None:
    text = files("vamos_agents.remi.prompt").joinpath("instructions.md").read_text(encoding="utf-8")
    names = {t.name for t in REGISTRY.specs()}
    assert "negotiate" not in text and "judge best" not in text and "counter-offer" not in text
    payments = text.split("# Payments and arrangements")[1].split("# Never")[0]
    for line in (
        "get_payment_options",
        "offer nothing else",
        "the full balance, then the plans, then a settlement",
        "lowest discount first",
        "request_approval before you mention it; if denied, never",
        "Never mention a percentage, a minimum, a limit",
        "Nothing fits (or they can't pay and the creditor allows no settlement): transfer_to_human",
    ):
        assert line in payments, line
    during = text.split("# During the conversation")[1].split("# Payments")[0]
    for intent, tool in (
        ("Dispute", "log_dispute"),
        ("Stop contacting me", 'record_flag "cease"'),
        ("Attorney", 'record_flag "attorney"'),
        ("Bankruptcy", 'record_flag "bankruptcy"'),
        ("Hardship", 'record_flag "hardship"'),
        ("Active military duty", "transfer_to_human"),
        ("Asked for a person", "transfer_to_human"),
    ):
        entry = during.split(f"- {intent}")[1].split("\n- ")[0]
        assert tool in entry, (intent, tool)
        assert tool.split()[0] in names
    for tool in (
        "get_payment_options",
        "request_approval",
        "take_payment",
        "create_arrangement",
        "record_promise",
    ):
        assert tool in names and tool in text


def test_the_money_tools_take_an_option_id_never_an_amount() -> None:
    specs = {t.name: t.parameters for t in REGISTRY.specs()}
    for tool in ("take_payment", "create_arrangement", "record_promise"):
        props = specs[tool]["properties"]
        assert "option_id" in props and "option_id" in specs[tool]["required"], tool
        assert not {"amount", "total", "instalment_count", "purpose", "kind"} & set(props), tool
    assert specs["request_approval"]["required"] == ["option_id"]
    assert specs["get_payment_options"]["properties"] == {}
