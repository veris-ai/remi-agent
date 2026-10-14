"""Money: payment methods, payments, arrangements and promises to pay.

All four need the debt unlocked (verified consumer, disclosures given) and are
refused once collection has stopped on the call.

The creditor's rules decide the money, not the model. Every payment, plan and
promise names one of the creditor's options (``get_payment_options``, the
World's decision engine) by its id, never a free amount: the amount and terms
are the option's. ``menu.checked_option`` refuses an id the menu does not list,
a settlement the creditor does not allow or that needs a recorded hardship,
and an approval tier without (or after a denied) ``request_approval``, before
anything is written or charged. Pay in full is the ledger's balance, as
before. Anything outside the menu goes to a person. A call takes at most one
charge: ``take_payment`` reserves it before anything is awaited (a concurrent
duplicate is refused), keeps it once the gateway approves, even when the ledger
posting then fails, and releases it only after a decline or a charge that never
happened. So an agreed amount is never charged twice.

Cards and bank accounts are saved and charged only through the collections
system's payment gateway (``payments.PaymentGateway``). Card and bank numbers
are accepted only by ``add_payment_method``, are declared ``redact`` (never
logged), go only to the gateway, and are never stored or echoed: the result
carries the gateway's ``method_id`` and the last four digits. The gateway
decides approve or decline.

The money itself is the ledger's (``ledger.FineractLedger``): every amount is
checked against the balance it holds, an approved charge is then posted to the
account's loan (and a one-payment settlement's remainder written off), and a
payment plan re-ages the loan, its schedule the one the ledger computed (and
refused if it is not the option's monthly amount). The agreement, its method
and its confirmation letter stay rows in Postgres; the World refuses an
off-menu one (``menu.PolicyRefused``), and that refusal reaches the model.

No amount crosses into these tools (an option id carries it), and every amount
they return is in dollars ("$250.00"): a model handed cents reads them out as
dollars. The World's rows and the gateway calls stay in cents.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall, new_id
from ..db import Transaction
from ..gates import COLLECTION, Gate
from ..ledger import Debt, LedgerUnavailable, Posting
from ..menu import PAY_IN_FULL, TO_A_PERSON, Option, PolicyRefused, checked_option, guarded_insert
from ..payments import BankDetails, CardDetails, Charge, PaymentGateway, SavedMethod
from .catalog import REGISTRY
from .options import current_menu

logger = logging.getLogger(__name__)

#: What each gateway decline code means, in words the model can say to the consumer.
DECLINE_REASONS = {
    "insufficient_funds": "there were not enough funds",
    "card_declined": "the card issuer declined it",
    "expired_card": "the card has expired",
}
DECLINE_NEXT = (
    "The payment did not go through; it is never described as having gone through, and this method is not "
    "charged again. Still open: another card or a bank account (add_payment_method, then take_payment), or a "
    "promise to pay the same option on an exact date no more than 14 days out (record_promise)."
)
OPTION_ID = (
    "The creditor's option (get_payment_options): 'pif' for the full balance, a settlement's option_id for that "
    "settlement, or a plan's option_id for the plan's down payment."
)
OUT_OF_STEP = (
    "The account's balance is not what this plan was priced on (the down payment may not have posted): no plan "
    "was set up. A person can handle it (transfer_to_human)."
)
NOT_POSTED = (
    "The charge went through, but posting it to the account's ledger failed: the payment is safe and a person "
    "will post it. Do not charge again."
)
SETTLED = (
    "Settled: the settlement covers the account, the rest is written off and nothing more is owed. There is no "
    "remaining balance to tell the consumer."
)
ALREADY_CHARGED = (
    "A payment already went through on this call; no second charge is taken. If its posting failed, a person "
    "posts it."
)


class PaymentMethodArgs(BaseModel):
    method: Literal["card", "ach"]
    card_number: str | None = Field(None, description="card: the full card number, digits only.")
    exp_month: int | None = Field(None, ge=1, le=12, description="card: expiry month, 1-12.")
    exp_year: int | None = Field(None, description="card: expiry year, four digits.")
    cvc: str | None = Field(None, description="card: the security code, digits only.")
    routing_number: str | None = Field(None, description="ach: the 9-digit routing number.")
    account_number: str | None = Field(None, description="ach: the bank account number, digits only.")


@REGISTRY.tool(
    PaymentMethodArgs,
    requires=Gate.DEBT_UNLOCKED,
    redact=("card_number", "exp_month", "exp_year", "cvc", "routing_number", "account_number"),
    tags=(COLLECTION,),
)
async def add_payment_method(session: RemiCall, args: PaymentMethodArgs) -> dict[str, Any]:
    """Save the card or bank account the consumer reads out with the payment gateway, so it can be charged
    (take_payment) or used for an arrangement (create_arrangement). Card details go only to the gateway. Never
    repeat a card number, expiry or security code aloud; refer to the method by its last four digits. If the
    gateway refuses the details, tell the consumer what was wrong and ask them to read them again."""
    session.require_consumer()
    consumer = session.consumer
    assert consumer is not None
    common = {"account_id": session.context.account_id, "holder_name": consumer.full_name}
    async with session.db.transaction() as tx:
        gateway = PaymentGateway(tx)
        if args.method == "card":
            if not args.card_number or not args.cvc or args.exp_month is None or args.exp_year is None:
                raise ToolError("A card needs the full number, expiry month and year, and security code.")
            card = CardDetails(args.card_number, args.exp_month, args.exp_year, args.cvc)
            saved = await gateway.tokenize_card(**common, card=card, at=session.clock.now())
        else:
            if not args.routing_number or not args.account_number:
                raise ToolError("A bank account needs the 9-digit routing number and the account number.")
            bank = BankDetails(args.routing_number, args.account_number)
            saved = await gateway.tokenize_ach(**common, bank=bank, at=session.clock.now())
    session.payment_methods[saved.method_id] = saved
    return {"method_id": saved.method_id, "method": saved.kind, "last4": saved.last4, "brand": saved.brand}


def _method(session: RemiCall, method_id: str) -> SavedMethod:
    saved = session.payment_methods.get(method_id)
    if saved is None:
        raise ToolError(
            "Unknown method_id: save the consumer's card or bank account with add_payment_method first."
        )
    return saved


def _described(saved: SavedMethod) -> str:
    return f"{'card' if saved.kind == 'card' else 'bank account'} ending {saved.last4}"


class PaymentArgs(BaseModel):
    method_id: str = Field(description="The method_id add_payment_method returned.")
    option_id: str = Field(description=OPTION_ID)


async def _open_debt(session: RemiCall) -> Debt:
    """The account's money, now; refused when the ledger says nothing is owed."""
    debt = await session.read_debt()
    if not debt.open or debt.balance_cents <= 0:
        raise ToolError("Nothing is owed on this account now: it has been paid or settled.")
    return debt


async def _option(session: RemiCall, option_id: str, debt: Debt) -> Option:
    """The option being paid or promised, if the creditor's rules allow it now. Pay in full is the ledger's
    balance and needs no menu; anything else is checked against the menu as the World answers it now."""
    if option_id == PAY_IN_FULL:
        return Option(PAY_IN_FULL, "pay_in_full", debt.balance_cents)
    menu = await current_menu(session, debt)
    return checked_option(
        menu, option_id, hardship_recorded=session.hardship_recorded, approvals=session.approvals
    )


def _charge_amount(session: RemiCall, option: Option) -> int:
    """What take_payment charges for an option: the full balance, a settlement, or a plan's down payment."""
    if option.kind != "plan":
        return option.amount_cents
    if not option.down_cents:
        raise ToolError(f"Plan {option.id} has no down payment: it is set up with create_arrangement.")
    if session.down_payment_for is not None:
        raise ToolError(f"The down payment for plan {session.down_payment_for.id} was already taken.")
    return option.down_cents


@REGISTRY.tool(PaymentArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def take_payment(session: RemiCall, args: PaymentArgs) -> dict[str, Any]:
    """Charge a saved payment method today for one of the creditor's options, after stating its amount and
    getting the consumer's agreement: the full balance ('pif'), a settlement, or a plan's down payment. The
    gateway approves or declines. A decline is real: the consumer hears it did not go through and why, never
    that it succeeded."""
    session.require_consumer()
    _claim_charge(session)  # before the first await
    try:
        debt = await _open_debt(session)
        balance = debt.balance_cents
        option = await _option(session, args.option_id, debt)
        amount = _charge_amount(session, option)
        if option.kind == "settlement" and not 0 < amount < balance:
            raise ToolError(f"Option {option.id} is not a settlement of this balance. {TO_A_PERSON}")
        saved = _method(session, args.method_id)
        if saved.method_id in session.declined_methods:
            raise ToolError(f"The {_described(saved)} was already declined on this call. {DECLINE_NEXT}")
        charge = await _charge(session, saved, amount)
        if charge.approved:
            session.charged_payment_id = charge.payment_id
    finally:
        session.charge_in_flight = False  # the charge is kept by charged_payment_id, or released
    if not charge.approved:
        session.declined_methods.add(saved.method_id)
        return _declined(saved, charge)
    settled = option.kind == "settlement"
    posted = await _post(session, debt, saved, charge, amount, settle=settled)
    result: dict[str, Any] = {
        "status": "approved",
        "amount": policy.dollars(amount),
        "paid_with": _described(saved),
        "consumer_has_heard": False,
        "allowed_now": "The charge went through, and the consumer does not know that yet: the outcome reaches "
        "them before anything else, including logging or ending the call.",
    }
    if settled:
        result["account"] = SETTLED  # the write-off leaves nothing owed: no remaining balance to say
    else:
        result["remaining_balance"] = policy.dollars(balance - amount)
    if option.kind == "plan":
        session.down_payment_for = option
        result["next"] = f"The plan itself: create_arrangement with option_id {option.id}."
    else:
        session.outcomes.add("settled" if settled else "paid")
    if settled and (refused := await _record_settlement(session, amount, saved)):
        result["settlement_record"] = refused
    if not posted:
        result["ledger"] = NOT_POSTED
    return result


async def _record_settlement(session: RemiCall, amount: int, saved: SavedMethod) -> str | None:
    """The one-payment settlement's agreement, after its approved charge. A World refusal now cannot undo the
    charge: it is logged (``guarded_insert``) and returned for a person to review, never hidden."""
    today = session.clock.today()
    try:
        async with session.db.transaction() as tx:
            await _insert_arrangement(
                session,
                tx,
                new_id("arr"),
                "settlement_lump",
                amount,
                [{"due_on": today.isoformat(), "amount_cents": amount}],
                saved,
            )
    except PolicyRefused as refused:
        return f"{refused} The payment itself went through; a person reviews the settlement record."
    return None


def _claim_charge(session: RemiCall) -> None:
    """Reserve the call's one charge without awaiting, so a concurrent take_payment sees it taken: refused
    while another is in flight or once one went through. The caller releases it unless the gateway charged."""
    if session.charged_payment_id is not None:
        raise ToolError(ALREADY_CHARGED)
    if session.charge_in_flight:
        raise ToolError("A payment is already being processed on this call: wait for its result.")
    session.charge_in_flight = True


async def _charge(session: RemiCall, saved: SavedMethod, amount: int) -> Charge:
    """Charge through the gateway. An error before the gateway answered leaves its transaction rolled back
    (nothing charged); one after it answered (the commit) leaves the outcome unknown, so the call is marked
    charged, never charged again, and the failure is logged and raised."""
    charge: Charge | None = None
    try:
        async with session.db.transaction() as tx:
            charge = await PaymentGateway(tx).charge(
                method_id=saved.method_id, amount_cents=amount, at=session.clock.now()
            )
    except BaseException:
        if charge is not None:
            session.charged_payment_id = charge.payment_id
            logger.error(
                "charge %s may or may not have committed: no further charge on this call", charge.payment_id
            )
        raise
    assert charge is not None
    return charge


async def _post(
    session: RemiCall, debt: Debt, saved: SavedMethod, charge: Charge, amount: int, *, settle: bool
) -> bool:
    """Post an approved charge to the account's loan, then, for a settlement, write off the rest. Each
    step is tried twice: the posting carries the gateway's payment id, so a second try never posts twice.
    False (and logged) when the ledger stayed out of reach: the money was taken and a person must post it."""
    today = session.clock.today()
    for attempt in (1, 2):
        try:
            posting: Posting = await session.ledger.post_payment(
                debt,
                amount_cents=amount,
                on=today,
                kind=saved.kind,
                payment_id=charge.payment_id,
                processor_ref=charge.processor_ref,
            )
            if settle:
                await session.ledger.write_off(
                    debt,
                    on=today,
                    external_id=f"wo_{charge.payment_id}",
                    note=f"Settled for {policy.dollars(amount)}: payment {charge.payment_id}.",
                )
            logger.info(
                "posted %s to loan %s as transaction %s",
                charge.payment_id,
                debt.loan_id,
                posting.transaction_id,
            )
            return True
        except LedgerUnavailable as err:
            logger.error(
                "posting %s to %s failed (attempt %d): %s", charge.payment_id, debt.account_id, attempt, err
            )
    return False


def _declined(saved: SavedMethod, charge: Charge) -> dict[str, Any]:
    code = charge.decline_code or "card_declined"
    return {
        "status": "declined",
        "decline_code": code,
        "reason": DECLINE_REASONS.get(code, code.replace("_", " ")),
        "declined": _described(saved),
        "allowed_now": DECLINE_NEXT,
    }


class ArrangementArgs(BaseModel):
    option_id: str = Field(description="The plan's option_id (get_payment_options).")
    method_id: str = Field(
        description="The method_id add_payment_method returned; the instalments are charged to it."
    )
    first_due_on: date = Field(
        description="Date of the first monthly payment, YYYY-MM-DD, within 30 days; later ones fall on the same "
        "day each month."
    )


async def _plan(session: RemiCall, option_id: str, debt: Debt) -> Option:
    """The plan option to set up, if the creditor's rules allow it, and the balance it was priced on is the
    ledger's now (after its down payment, if it has one)."""
    if session.down_payment_for is not None and session.down_payment_for.id == option_id:
        option = session.down_payment_for  # the terms agreed before the down payment changed the balance
    else:
        option = await _option(session, option_id, debt)
        if option.kind != "plan":
            raise ToolError(
                "create_arrangement sets up a plan option; the full balance and settlements are take_payment or "
                "record_promise."
            )
        if option.down_cents:
            raise ToolError(
                f"Plan {option.id} starts with a down payment of {policy.dollars(option.down_cents)}: take_payment "
                "with this option_id first, then create_arrangement."
            )
    if debt.balance_cents != option.amount_cents - option.down_cents:
        logger.error(
            "plan %s on %s: priced on %d cents, the ledger holds %d",
            option.id,
            session.context.account_id,
            option.amount_cents - option.down_cents,
            debt.balance_cents,
        )
        raise ToolError(OUT_OF_STEP)
    return option


@REGISTRY.tool(ArrangementArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def create_arrangement(session: RemiCall, args: ArrangementArgs) -> dict[str, Any]:
    """Set up one of the creditor's plan options the consumer agreed to, after stating its terms (months, each
    monthly amount, the first date). A plan with a down payment needs take_payment for that option first. Queues
    the confirmation letter. Terms the creditor's options do not list go to a person."""
    session.require_consumer()
    debt = await _open_debt(session)
    option = await _plan(session, args.option_id, debt)
    if refusal := policy.first_due_refusal(args.first_due_on, session.clock.today()):
        raise ToolError(f"{refusal.detail}.")
    saved = _method(session, args.method_id)
    instalments = await _re_age(session, debt, option, args.first_due_on, saved)
    session.outcomes.add("plan")
    return {
        "created": "plan",
        "option_id": option.id,
        "total": policy.dollars(debt.balance_cents),
        "instalments": [
            {"due_on": i["due_on"], "amount": policy.dollars(i["amount_cents"])} for i in instalments
        ],
        "charged_to": _described(saved),
        "confirmation_letter": "queued",
    }


async def _re_age(
    session: RemiCall, debt: Debt, option: Option, first_due_on: date, saved: SavedMethod
) -> list[dict[str, Any]]:
    """Put the balance on the plan in the ledger, then record the agreement with the ledger's own schedule;
    if the schedule is not the option's (its months, its monthly amount) or the agreement cannot be recorded,
    the re-age is undone, so the ledger, the record and what the consumer was told never disagree."""
    assert option.months is not None
    arrangement_id = new_id("arr")
    aged = await session.ledger.re_age(
        debt, instalments=option.months, first_due_on=first_due_on, external_id=arrangement_id
    )
    session.debt = aged
    schedule = [i for i in aged.schedule if i.due_on >= first_due_on and i.paid_cents < i.amount_cents]
    instalments = [
        {"due_on": i.due_on.isoformat(), "amount_cents": i.amount_cents - i.paid_cents} for i in schedule
    ]
    try:
        if len(instalments) != option.months or instalments[0]["amount_cents"] != option.monthly_cents:
            logger.error(
                "plan %s on %s: the ledger's schedule %s is not %d x %s cents",
                option.id,
                session.context.account_id,
                [i["amount_cents"] for i in instalments],
                option.months,
                option.monthly_cents,
            )
            raise ToolError(
                f"The ledger's schedule is not plan {option.id}'s terms: no plan was set up. A person can handle "
                "it (transfer_to_human)."
            )
        async with session.db.transaction() as tx:
            await _insert_arrangement(
                session, tx, arrangement_id, "plan", debt.balance_cents, instalments, saved
            )
    except BaseException:
        await session.ledger.undo_re_age(aged)
        raise
    return instalments


async def _insert_arrangement(
    session: RemiCall,
    tx: Transaction,
    arrangement_id: str,
    kind: str,
    total: int,
    instalments: list[dict[str, Any]],
    saved: SavedMethod,
) -> None:
    await guarded_insert(
        tx,
        "arrangements",
        {
            "arrangement_id": arrangement_id,
            "account_id": session.context.account_id,
            "kind": kind,
            "total_cents": total,
            "instalments": instalments,
            "method_id": saved.method_id,
            "created_at": session.clock.now(),
            "confirmation_letter_due_by": policy.confirmation_letter_due_by(session.clock.today()),
        },
    )
    await session.queue_letters(tx, "plan_confirmation")


class PromiseArgs(BaseModel):
    option_id: str = Field(
        description="The creditor's option promised (get_payment_options): 'pif' for the full balance, or a "
        "settlement's option_id."
    )
    due_on: date = Field(
        description="The exact date, YYYY-MM-DD, no more than 14 days from today (a settlement: by its pay_by)."
    )
    method: Literal["card", "ach", "check"]


@REGISTRY.tool(PromiseArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def record_promise(session: RemiCall, args: PromiseArgs) -> dict[str, Any]:
    """Record a promise to pay one of the creditor's options (the full balance or a settlement) on an exact date
    at most 14 days out, and how they will pay. Confirm the amount, date and method back to the consumer first.
    'Soon' or 'next week' is not a promise."""
    session.require_consumer()
    account = session.account
    assert account is not None
    today = session.clock.today()
    if refusal := policy.ptp_refusal(args.due_on, today):
        raise ToolError(refusal.detail)
    debt = await _open_debt(session)
    option = await _option(session, args.option_id, debt)
    if option.kind == "plan":
        raise ToolError("A plan is set up with create_arrangement, not promised.")
    if option.due_within_days is not None and args.due_on > today + timedelta(days=option.due_within_days):
        pay_by = today + timedelta(days=option.due_within_days)
        raise ToolError(f"Option {option.id} must be paid by {pay_by.isoformat()}.")
    amount = option.amount_cents
    async with session.db.transaction() as tx:
        await tx.insert(
            "promises_to_pay",
            {
                "promise_id": new_id("ptp"),
                "account_id": account.account_id,
                "amount_cents": amount,
                "due_on": args.due_on,
                "method": args.method,
                "created_at": session.clock.now(),
            },
        )
    session.outcomes.add("ptp")
    return {
        "recorded": "promise",
        "amount": policy.dollars(amount),
        "due_on": args.due_on.isoformat(),
        "method": args.method,
    }
