"""Money: payment methods, payments, arrangements and promises to pay.

All four need the debt unlocked (verified consumer, disclosures given) and are
refused once collection has stopped on the call. The house rules in
``policy`` decide every amount, term and date; a request outside them comes
back as an error telling the model what to do instead: a settlement under the
minimum is countered, starting near the balance and stepping down to the minimum, and
any other term outside the rules goes to a person.

Cards and bank accounts are saved and charged only through the collections
system's payment gateway (``payments.PaymentGateway``). Card and bank numbers
are accepted only by ``add_payment_method``, are declared ``redact`` (never
logged), go only to the gateway, and are never stored or echoed: the result
carries the gateway's ``method_id`` and the last four digits. The gateway
decides approve or decline.

The money itself is the ledger's (``ledger.FineractLedger``): every amount is
checked against the balance it holds, an approved charge is then posted to the
account's loan (and a one-payment settlement's remainder written off), and a
payment plan re-ages the loan, its schedule the one the ledger computed. The
agreement, its method and its confirmation letter stay rows in Postgres.

Amounts cross the tool boundary in dollars, both ways (arguments as numbers
like 250.00, results as "$250.00"): a model handed cents reads them out as
dollars. The World's rows and the gateway calls stay in cents.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall, new_id
from ..db import Transaction
from ..gates import COLLECTION, Gate
from ..ledger import Debt, LedgerUnavailable, Posting
from ..payments import BankDetails, CardDetails, Charge, PaymentGateway, SavedMethod
from .catalog import REGISTRY

logger = logging.getLogger(__name__)

TRANSFER_HINT = "No counter-offer is allowed; a person can handle it (transfer_to_human)."
COUNTER_HINT = (
    "Don't accept it: counter-offer, starting near the full balance and coming down in steps, never below the "
    "minimum. If the consumer will not reach the minimum, a person can handle it (transfer_to_human)."
)


def _refused(refusal: policy.Refusal) -> ToolError:
    """A settlement under the minimum is countered on the call; any other term outside the rules goes to a person."""
    return ToolError(f"{refusal.detail}. {COUNTER_HINT if refusal.code == 'below_floor' else TRANSFER_HINT}")


#: What each gateway decline code means, in words the model can say to the consumer.
DECLINE_REASONS = {
    "insufficient_funds": "there were not enough funds",
    "card_declined": "the card issuer declined it",
    "expired_card": "the card has expired",
}
DECLINE_NEXT = (
    "The payment did not go through; it is never described as having gone through, and this method is not "
    "charged again. Still open: another card or a bank account (add_payment_method, then take_payment), or a "
    "promise to pay: an exact amount on an exact date no more than 14 days out (record_promise)."
)
DOLLARS = "In dollars, e.g. 250.00."
NOT_POSTED = (
    "The charge went through, but posting it to the account's ledger failed: the payment is safe and a person "
    "will post it. Do not charge again."
)


def _cents(amount: float) -> int:
    cents = policy.to_cents(amount)
    if cents is None:
        raise ToolError("Amounts are dollars and cents, e.g. 250.00.")
    return cents


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
    amount: float = Field(gt=0, description=DOLLARS)
    purpose: Literal["pay_in_full", "settlement"] = Field(
        description="pay_in_full: the whole current balance. settlement: one payment of an agreed settlement."
    )


async def _open_debt(session: RemiCall) -> Debt:
    """The account's money, now; refused when the ledger says nothing is owed."""
    debt = await session.read_debt()
    if not debt.open or debt.balance_cents <= 0:
        raise ToolError("Nothing is owed on this account now: it has been paid or settled.")
    return debt


@REGISTRY.tool(PaymentArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def take_payment(session: RemiCall, args: PaymentArgs) -> dict[str, Any]:
    """Charge a saved payment method today, after stating the amount and getting the consumer's agreement:
    the full balance (pay_in_full) or a one-payment settlement at or above the settlement minimum. The
    gateway approves or declines. A decline is real: tell the consumer it did not go through and why, never
    that it succeeded."""
    session.require_consumer()
    account = session.account
    assert account is not None
    debt = await _open_debt(session)
    balance = debt.balance_cents
    amount = _cents(args.amount)
    if args.purpose == "pay_in_full" and amount != balance:
        raise ToolError(f"Paying in full means exactly the balance, {policy.dollars(balance)}.")
    if args.purpose == "settlement" and (refusal := policy.settlement_refusal(account, balance, amount, 1)):
        raise _refused(refusal)
    saved = _method(session, args.method_id)
    if saved.method_id in session.declined_methods:
        raise ToolError(f"The {_described(saved)} was already declined on this call. {DECLINE_NEXT}")
    async with session.db.transaction() as tx:
        charge = await PaymentGateway(tx).charge(
            method_id=saved.method_id, amount_cents=amount, at=session.clock.now()
        )
    if not charge.approved:
        session.declined_methods.add(saved.method_id)
        return _declined(saved, charge)
    settled = args.purpose == "settlement"
    posted = await _post(session, debt, saved, charge, amount, settle=settled)
    if settled:
        today = session.clock.today()
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
    outcome = "settled" if settled else "paid"
    session.outcomes.add(outcome)
    result: dict[str, Any] = {
        "status": "approved",
        "amount": policy.dollars(amount),
        "remaining_balance": policy.dollars(balance - amount),
        "paid_with": _described(saved),
        "consumer_has_heard": False,
        "allowed_now": "The charge went through, and the consumer does not know that yet: the outcome reaches "
        "them before anything else, including logging or ending the call.",
    }
    if not posted:
        result["ledger"] = NOT_POSTED
    return result


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
    kind: Literal["plan", "hardship_plan", "settlement_instalments"]
    method_id: str = Field(
        description="The method_id add_payment_method returned; the instalments are charged to it."
    )
    instalment_count: int = Field(gt=0, description="Number of equal monthly payments.")
    first_due_on: date = Field(
        description="Date of the first payment, YYYY-MM-DD; later ones fall on the same day each month."
    )
    total: float | None = Field(
        None,
        gt=0,
        description="settlement_instalments only: the agreed settlement total, in dollars, e.g. 900.00.",
    )


@REGISTRY.tool(ArrangementArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def create_arrangement(session: RemiCall, args: ArrangementArgs) -> dict[str, Any]:
    """Set up an arrangement the consumer agreed to, after stating its terms: a payment plan for the full
    balance, a hardship plan (only after recording a hardship), or a settlement paid in up to 3 monthly
    instalments. Queues the confirmation letter. Terms outside the house rules are refused: offer a person."""
    session.require_consumer()
    account = session.account
    assert account is not None
    today = session.clock.today()
    debt = await _open_debt(session)
    balance = debt.balance_cents
    if args.kind == "settlement_instalments":
        if args.total is None:
            raise ToolError("A settlement in instalments needs its agreed total.")
        total = _cents(args.total)
        refusal = policy.settlement_refusal(
            account, balance, total, args.instalment_count
        ) or policy.first_due_refusal(args.first_due_on, today)
    else:
        if args.kind == "hardship_plan" and not session.hardship_recorded:
            raise ToolError(
                "A hardship plan needs the hardship recorded first (record_flag kind 'hardship')."
            )
        total = balance
        refusal = policy.plan_refusal(
            account,
            balance,
            args.instalment_count,
            args.first_due_on,
            today,
            hardship=args.kind == "hardship_plan",
        )
    if refusal:
        raise _refused(refusal)
    saved = _method(session, args.method_id)
    arrangement_id = new_id("arr")
    if args.kind == "settlement_instalments":
        # An agreement until its last instalment: the ledger is written off only when it is paid.
        instalments = policy.monthly_instalments(total, args.instalment_count, args.first_due_on)
        async with session.db.transaction() as tx:
            await _insert_arrangement(session, tx, arrangement_id, args.kind, total, instalments, saved)
    else:
        instalments = await _re_age(session, debt, args, arrangement_id, saved)
    session.outcomes.add("settlement_plan" if args.kind == "settlement_instalments" else args.kind)
    return {
        "created": args.kind,
        "total": policy.dollars(total),
        "instalments": [
            {"due_on": i["due_on"], "amount": policy.dollars(i["amount_cents"])} for i in instalments
        ],
        "charged_to": _described(saved),
        "confirmation_letter": "queued",
    }


async def _re_age(
    session: RemiCall, debt: Debt, args: ArrangementArgs, arrangement_id: str, saved: SavedMethod
) -> list[dict[str, Any]]:
    """Put the balance on the plan in the ledger, then record the agreement with the ledger's own schedule;
    if the agreement cannot be recorded, the re-age is undone, so the two never disagree."""
    aged = await session.ledger.re_age(
        debt, instalments=args.instalment_count, first_due_on=args.first_due_on, external_id=arrangement_id
    )
    session.debt = aged
    schedule = [i for i in aged.schedule if i.due_on >= args.first_due_on and i.paid_cents < i.amount_cents]
    instalments = [
        {"due_on": i.due_on.isoformat(), "amount_cents": i.amount_cents - i.paid_cents} for i in schedule
    ]
    try:
        if len(instalments) != args.instalment_count:
            raise ToolError(
                f"The ledger produced {len(instalments)} payments, not {args.instalment_count}: no plan was set up. "
                "Offer a person (transfer_to_human)."
            )
        async with session.db.transaction() as tx:
            await _insert_arrangement(
                session, tx, arrangement_id, args.kind, debt.balance_cents, instalments, saved
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
    await tx.insert(
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
    amount: float = Field(gt=0, description=DOLLARS)
    due_on: date = Field(description="The exact date, YYYY-MM-DD, no more than 14 days from today.")
    method: Literal["card", "ach", "check"]


@REGISTRY.tool(PromiseArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def record_promise(session: RemiCall, args: PromiseArgs) -> dict[str, Any]:
    """Record a promise to pay: an exact amount on an exact date at most 14 days out, and how they will pay.
    Confirm the amount, date and method back to the consumer first. 'Soon' or 'next week' is not a promise."""
    session.require_consumer()
    account = session.account
    assert account is not None
    if refusal := policy.ptp_refusal(args.due_on, session.clock.today()):
        raise ToolError(refusal.detail)
    amount = _cents(args.amount)
    debt = await _open_debt(session)
    if amount > debt.balance_cents:
        raise ToolError(f"A promise cannot exceed the balance, {policy.dollars(debt.balance_cents)}.")
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
