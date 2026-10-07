"""Money: payment methods, payments, arrangements and promises to pay.

All four need the debt unlocked (verified consumer, disclosures given) and are
refused once collection has stopped on the call. The house rules in
``policy`` decide every amount, term and date; a request outside them comes
back as an error telling the model to offer a person instead of inventing a
counter-offer.

Cards and bank accounts are saved and charged only through the collections
system's payment gateway (``payments.PaymentGateway``). Card and bank numbers
are accepted only by ``add_payment_method``, are declared ``redact`` (never
logged), go only to the gateway, and are never stored or echoed: the result
carries the gateway's ``method_id`` and the last four digits. The gateway
decides approve or decline, and applies an approved charge to the balance
itself; Remi sets only the account's ``status``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall, new_id
from ..db import Transaction
from ..gates import COLLECTION, Gate
from ..payments import BankDetails, CardDetails, Charge, PaymentGateway, SavedMethod
from .catalog import REGISTRY

TRANSFER_HINT = "Do not counter-offer. Offer to transfer them to a person (transfer_to_human)."

#: What each gateway decline code means, in words the model can say to the consumer.
DECLINE_REASONS = {
    "insufficient_funds": "there were not enough funds",
    "card_declined": "the card issuer declined it",
    "expired_card": "the card has expired",
}
DECLINE_NEXT = (
    "Tell the consumer the payment did not go through and why; never say it succeeded, and do not charge "
    "this method again. You may offer another card or a bank account (add_payment_method, then take_payment), "
    "or a promise to pay: an exact amount on an exact date no more than 14 days out (record_promise)."
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
                raise ToolError("Ask for the full card number, expiry month and year, and security code.")
            card = CardDetails(args.card_number, args.exp_month, args.exp_year, args.cvc)
            saved = await gateway.tokenize_card(**common, card=card, at=session.clock.now())
        else:
            if not args.routing_number or not args.account_number:
                raise ToolError("Ask for the 9-digit routing number and the account number.")
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
    amount_cents: int = Field(gt=0)
    purpose: Literal["pay_in_full", "settlement"] = Field(
        description="pay_in_full: the whole current balance. settlement: one payment of an agreed settlement."
    )


@REGISTRY.tool(PaymentArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def take_payment(session: RemiCall, args: PaymentArgs) -> dict[str, Any]:
    """Charge a saved payment method today, after stating the amount and getting the consumer's agreement:
    the full balance (pay_in_full) or a one-payment settlement at or above the settlement minimum. The
    gateway approves or declines. A decline is real: tell the consumer it did not go through and why, never
    that it succeeded."""
    session.require_consumer()
    account = session.account
    assert account is not None
    balance = account.current_balance_cents
    if args.purpose == "pay_in_full" and args.amount_cents != balance:
        raise ToolError(f"Paying in full means exactly the balance, {balance} cents.")
    if args.purpose == "settlement" and (refusal := policy.settlement_refusal(account, args.amount_cents, 1)):
        raise ToolError(f"{refusal.detail}. {TRANSFER_HINT}")
    saved = _method(session, args.method_id)
    if saved.method_id in session.declined_methods:
        raise ToolError(f"The {_described(saved)} was already declined on this call. {DECLINE_NEXT}")
    status = "paid" if args.purpose == "pay_in_full" else "settled"
    async with session.db.transaction() as tx:
        charge = await PaymentGateway(tx).charge(
            method_id=saved.method_id, amount_cents=args.amount_cents, at=session.clock.now()
        )
        if charge.approved:
            await tx.update("accounts", account.account_id, {"status": status})
            if status == "settled":
                today = session.clock.today()
                await _insert_arrangement(
                    session,
                    tx,
                    "settlement_lump",
                    args.amount_cents,
                    [{"due_on": today.isoformat(), "amount_cents": args.amount_cents}],
                    saved,
                )
    if not charge.approved:
        session.declined_methods.add(saved.method_id)
        return _declined(saved, charge)
    # The gateway applied the charge to the balance in the same transaction.
    session.account = replace(
        account,
        current_balance_cents=balance - args.amount_cents,
        paid_since_charge_off_cents=account.paid_since_charge_off_cents + args.amount_cents,
        last_payment_on=session.clock.today(),
        status=status,
    )
    session.outcomes.add(status)
    return {
        "status": "approved",
        "amount_cents": args.amount_cents,
        "remaining_balance_cents": balance - args.amount_cents,
        "paid_with": _described(saved),
    }


def _declined(saved: SavedMethod, charge: Charge) -> dict[str, Any]:
    code = charge.decline_code or "card_declined"
    return {
        "status": "declined",
        "decline_code": code,
        "reason": DECLINE_REASONS.get(code, code.replace("_", " ")),
        "declined": _described(saved),
        "next": DECLINE_NEXT,
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
    total_cents: int | None = Field(
        None, gt=0, description="settlement_instalments only: the agreed settlement total."
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
    if args.kind == "settlement_instalments":
        if args.total_cents is None:
            raise ToolError("A settlement in instalments needs total_cents.")
        total = args.total_cents
        refusal = policy.settlement_refusal(
            account, total, args.instalment_count
        ) or policy.first_due_refusal(args.first_due_on, today)
    else:
        if args.kind == "hardship_plan" and not session.hardship_recorded:
            raise ToolError(
                "Record the hardship first (record_flag kind 'hardship'), then offer the hardship plan."
            )
        total = account.current_balance_cents
        refusal = policy.plan_refusal(
            account, args.instalment_count, args.first_due_on, today, hardship=args.kind == "hardship_plan"
        )
    if refusal:
        raise ToolError(f"{refusal.detail}. {TRANSFER_HINT}")
    saved = _method(session, args.method_id)
    instalments = policy.monthly_instalments(total, args.instalment_count, args.first_due_on)
    async with session.db.transaction() as tx:
        await _insert_arrangement(session, tx, args.kind, total, instalments, saved)
    session.outcomes.add("settlement_plan" if args.kind == "settlement_instalments" else args.kind)
    return {
        "created": args.kind,
        "total_cents": total,
        "instalments": instalments,
        "charged_to": _described(saved),
        "confirmation_letter": "queued",
    }


async def _insert_arrangement(
    session: RemiCall,
    tx: Transaction,
    kind: str,
    total: int,
    instalments: list[dict[str, Any]],
    saved: SavedMethod,
) -> None:
    await tx.insert(
        "arrangements",
        {
            "arrangement_id": new_id("arr"),
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
    amount_cents: int = Field(gt=0)
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
    if args.amount_cents > account.current_balance_cents:
        raise ToolError(f"A promise cannot exceed the balance, {account.current_balance_cents} cents.")
    async with session.db.transaction() as tx:
        await tx.insert(
            "promises_to_pay",
            {
                "promise_id": new_id("ptp"),
                "account_id": account.account_id,
                "amount_cents": args.amount_cents,
                "due_on": args.due_on,
                "method": args.method,
                "created_at": session.clock.now(),
            },
        )
    session.outcomes.add("ptp")
    return {
        "recorded": "promise",
        "amount_cents": args.amount_cents,
        "due_on": args.due_on.isoformat(),
        "method": args.method,
    }
