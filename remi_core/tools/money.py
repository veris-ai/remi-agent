"""Money: payment methods, payments, arrangements and promises to pay.

All four need the debt unlocked (verified consumer, disclosures given) and are
refused once collection has stopped on the call. The house rules in
``policy`` decide every amount, term and date; a request outside them comes
back as an error telling the model to offer a person instead of inventing a
counter-offer.

Card and bank numbers are accepted only by ``add_payment_method``, are
declared ``redact`` (never logged), go only to the payment gateway, and are
never stored or echoed: the result carries the PaymentMethod id and last four.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import policy
from ..db import Transaction
from ..payments import BankDetails, CardDetails, SavedMethod
from ..session import CallSession, new_id
from ..tool_registry import Gate, ToolError
from .catalog import REGISTRY

TRANSFER_HINT = "Do not counter-offer. Offer to transfer them to a person (transfer_to_human)."


class PaymentMethodArgs(BaseModel):
    method: Literal["card", "ach"]
    card_number: str | None = Field(None, description="card: the full card number, digits only.")
    exp_month: int | None = Field(None, ge=1, le=12, description="card: expiry month, 1-12.")
    exp_year: int | None = Field(None, description="card: expiry year, four digits.")
    cvc: str | None = Field(None, description="card: the security code, digits only.")
    routing_number: str | None = Field(None, description="ach: the 9-digit routing number.")
    account_number: str | None = Field(None, description="ach: the bank account number, digits only.")


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


@REGISTRY.tool(
    PaymentMethodArgs,
    requires=Gate.DEBT_UNLOCKED,
    redact=("card_number", "exp_month", "exp_year", "cvc", "routing_number", "account_number"),
    blocked_by_stop=True,
)
async def add_payment_method(session: CallSession, args: PaymentMethodArgs) -> dict[str, Any]:
    """Save the card or bank account the consumer reads out, so it can be charged (take_payment) or used for an
    arrangement (create_arrangement). Card details go only to the payment processor. Never repeat a card
    number, expiry or security code aloud; refer to the method by its last four digits."""
    session.require_consumer()
    consumer, account = session.consumer, session.account
    assert consumer is not None and account is not None
    customer_id = await _customer(session)
    if args.method == "card":
        number, cvc = _digits(args.card_number), _digits(args.cvc)
        if (
            not (13 <= len(number) <= 19)
            or not (3 <= len(cvc) <= 4)
            or args.exp_month is None
            or args.exp_year is None
        ):
            raise ToolError("Ask for the full card number, expiry month and year, and security code.")
        exp_year = args.exp_year + 2000 if args.exp_year < 100 else args.exp_year
        saved = await session.payments.save_card(
            customer_id=customer_id,
            name=consumer.full_name,
            card=CardDetails(number, args.exp_month, exp_year, cvc),
        )
    else:
        routing, number = _digits(args.routing_number), _digits(args.account_number)
        if len(routing) != 9 or not (4 <= len(number) <= 17):
            raise ToolError("Ask for the 9-digit routing number and the account number.")
        saved = await session.payments.save_bank_account(
            customer_id=customer_id, name=consumer.full_name, bank=BankDetails(routing, number)
        )
    session.payment_methods[saved.payment_method_id] = saved
    return {
        "payment_method_id": saved.payment_method_id,
        "method": saved.method,
        "last4": saved.last4,
        "brand": saved.brand,
    }


async def _customer(session: CallSession) -> str:
    """The account's Stripe customer, created (and written to the account) on first use."""
    account, consumer = session.account, session.consumer
    assert account is not None and consumer is not None
    if account.stripe_customer_id:
        return account.stripe_customer_id
    customer_id = await session.payments.create_customer(
        account_id=account.account_id, name=consumer.full_name
    )
    async with session.db.transaction() as tx:
        await tx.update("accounts", account.account_id, {"stripe_customer_id": customer_id})
    session.account = replace(account, stripe_customer_id=customer_id)
    return customer_id


def _method(session: CallSession, payment_method_id: str) -> SavedMethod:
    saved = session.payment_methods.get(payment_method_id)
    if saved is None:
        raise ToolError(
            "Unknown payment_method_id: save the consumer's card or bank account with add_payment_method first."
        )
    return saved


class PaymentArgs(BaseModel):
    payment_method_id: str
    amount_cents: int = Field(gt=0)
    purpose: Literal["pay_in_full", "settlement"] = Field(
        description="pay_in_full: the whole current balance. settlement: one payment of an agreed settlement."
    )


@REGISTRY.tool(PaymentArgs, requires=Gate.DEBT_UNLOCKED, blocked_by_stop=True)
async def take_payment(session: CallSession, args: PaymentArgs) -> dict[str, Any]:
    """Charge a saved payment method today, after stating the amount and getting the consumer's agreement:
    the full balance (pay_in_full) or a one-payment settlement at or above the settlement minimum. A decline
    is real: tell the consumer it did not go through, never that it succeeded."""
    session.require_consumer()
    account = session.account
    assert account is not None
    balance = account.current_balance_cents
    if args.purpose == "pay_in_full" and args.amount_cents != balance:
        raise ToolError(f"Paying in full means exactly the balance, {balance} cents.")
    if args.purpose == "settlement" and (refusal := policy.settlement_refusal(account, args.amount_cents, 1)):
        raise ToolError(f"{refusal.detail}. {TRANSFER_HINT}")
    saved = _method(session, args.payment_method_id)
    customer_id = await _customer(session)
    account = session.account  # now carries the Stripe customer id
    assert account is not None
    result = await session.payments.charge(
        customer_id=customer_id, method=saved, amount_cents=args.amount_cents, account_id=account.account_id
    )
    now, today = session.clock.now(), session.clock.today()
    async with session.db.transaction() as tx:
        await tx.insert(
            "payments",
            {
                "payment_id": new_id("pay"),
                "account_id": account.account_id,
                "stripe_payment_intent_id": result.payment_intent_id,
                "amount_cents": args.amount_cents,
                "status": result.status,
                "at": now,
            },
        )
        if result.status == "succeeded":
            status = "paid" if args.purpose == "pay_in_full" else "settled"
            changes = {
                "current_balance_cents": balance - args.amount_cents,
                "paid_since_charge_off_cents": account.paid_since_charge_off_cents + args.amount_cents,
                "last_payment_on": today,
                "status": status,
            }
            await tx.update("accounts", account.account_id, changes)
            if status == "settled":
                await _insert_arrangement(
                    session,
                    tx,
                    "settlement_lump",
                    args.amount_cents,
                    [{"due_on": today.isoformat(), "amount_cents": args.amount_cents}],
                    saved,
                )
    if result.status != "succeeded":
        return {
            "status": "failed",
            "decline_message": result.decline_message,
            "next": "Tell the consumer the payment did not go through. You may offer another card or bank account, "
            "or a promise to pay.",
        }
    session.account = replace(account, **changes)
    session.outcomes.add("paid" if args.purpose == "pay_in_full" else "settled")
    return {
        "status": "succeeded",
        "amount_cents": args.amount_cents,
        "remaining_balance_cents": balance - args.amount_cents,
    }


class ArrangementArgs(BaseModel):
    kind: Literal["plan", "hardship_plan", "settlement_instalments"]
    payment_method_id: str
    instalment_count: int = Field(gt=0, description="Number of equal monthly payments.")
    first_due_on: date = Field(
        description="Date of the first payment, YYYY-MM-DD; later ones fall on the same day each month."
    )
    total_cents: int | None = Field(
        None, gt=0, description="settlement_instalments only: the agreed settlement total."
    )


@REGISTRY.tool(ArrangementArgs, requires=Gate.DEBT_UNLOCKED, blocked_by_stop=True)
async def create_arrangement(session: CallSession, args: ArrangementArgs) -> dict[str, Any]:
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
    saved = _method(session, args.payment_method_id)
    instalments = policy.monthly_instalments(total, args.instalment_count, args.first_due_on)
    async with session.db.transaction() as tx:
        await _insert_arrangement(session, tx, args.kind, total, instalments, saved)
    session.outcomes.add("settlement_plan" if args.kind == "settlement_instalments" else args.kind)
    return {
        "created": args.kind,
        "total_cents": total,
        "instalments": instalments,
        "confirmation_letter": "queued",
    }


async def _insert_arrangement(
    session: CallSession,
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
            "stripe_payment_method_id": saved.payment_method_id,
            "created_at": session.clock.now(),
            "confirmation_letter_due_by": policy.confirmation_letter_due_by(session.clock.today()),
        },
    )
    await session.queue_letters(tx, "plan_confirmation")


class PromiseArgs(BaseModel):
    amount_cents: int = Field(gt=0)
    due_on: date = Field(description="The exact date, YYYY-MM-DD, no more than 14 days from today.")
    method: Literal["card", "ach", "check"]


@REGISTRY.tool(PromiseArgs, requires=Gate.DEBT_UNLOCKED, blocked_by_stop=True)
async def record_promise(session: CallSession, args: PromiseArgs) -> dict[str, Any]:
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
