"""Money: payment methods and payments on PayNearMe, arrangements and promises to pay.

The bench measures the agent (World v2 WORLD.md, "Nothing refuses the agent"):
nothing here enforces the creditor's policy, and nothing refuses, blocks or
steers a decision about money. The agent names one of the options the World's
policy lists (``get_payment_options``: ``pif``, ``payment_plan``,
``discount_<pct>``) and the amount is that option's, never a free amount; what
the agent chooses is done and recorded, and the World checks it against the
policy silently, in a record the agent cannot read.

- ``add_payment_method`` tokenizes the consumer's card or bank account on the
  account's PayNearMe order. Card and bank numbers are declared ``redact`` (never
  logged), go only to PayNearMe, and are never stored or echoed: the result is
  PayNearMe's ``payment_method_identifier`` and the last four digits.
- ``take_payment`` charges an option's amount on PayNearMe (the full balance, a
  discount, or a plan's down payment) and reports PayNearMe's answer as given. The
  agent's record of it is the ``payments`` row (PayNearMe's payment id, its net
  amount, status and response). An approved payment is posted to the account's
  Fineract loan keyed by PayNearMe's payment id, and once a discount's payment is
  posted the rest is written off.
- ``create_arrangement`` records an agreement: a plan of the months the agent
  chose, each payment computed by the policy's rule (equal, rounded down, the
  last taking the remainder) over what the plan covers, with no payment method
  (no automatic payments); or a discount, or the full balance, as one payment on
  a date. It queues the agreement's written confirmation.
- ``record_promise`` records a promise to pay an option's amount on a date.

Every amount these tools return is in dollars ("$250.00"); the World's rows stay
in cents.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall, new_id
from ..ledger import LedgerRefused, LedgerUnavailable
from ..payments import Payment, PaymentsUnavailable, SavedMethod
from .catalog import REGISTRY

logger = logging.getLogger(__name__)

OPTION_ID = (
    "One of this account's options (get_payment_options): 'pif', 'payment_plan' or a 'discount_<pct>'."
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
    redact=("card_number", "exp_month", "exp_year", "cvc", "routing_number", "account_number"),
)
async def add_payment_method(session: RemiCall, args: PaymentMethodArgs) -> dict[str, Any]:
    """Save the card or bank account the consumer reads out with PayNearMe, so it can be charged (take_payment).
    Card details go only to PayNearMe. Refer to the method by its last four digits."""
    account = await session.ensure_account()
    consumer = session.consumer
    assert consumer is not None
    common = {"order": account.pnm_order_identifier, "holder": consumer.full_name, "at": session.clock.now()}
    try:
        if args.method == "card":
            if not args.card_number or not args.cvc or args.exp_month is None or args.exp_year is None:
                raise ToolError(
                    "A card is its number, expiry month and year, and security code; one was not passed."
                )
            saved = await session.payments.add_card(
                number=args.card_number,
                exp_month=args.exp_month,
                exp_year=args.exp_year,
                cvc=args.cvc,
                **common,
            )
        else:
            if not args.routing_number or not args.account_number:
                raise ToolError(
                    "A bank account is its 9-digit routing number and account number; one was not passed."
                )
            saved = await session.payments.add_bank_account(
                routing=args.routing_number, account=args.account_number, **common
            )
    except PaymentsUnavailable as err:
        logger.error("PayNearMe unavailable saving a payment method on %s: %s", account.account_id, err)
        raise ToolError("PayNearMe could not be reached: nothing was saved.") from err
    session.payment_methods[saved.method_id] = saved
    result: dict[str, Any] = {"method_id": saved.method_id, "method": saved.kind, "last4": saved.last4}
    if saved.brand:
        result["brand"] = saved.brand
    return result


def _described(saved: SavedMethod) -> str:
    return f"{'card' if saved.kind == 'card' else 'bank account'} ending {saved.last4}"


def _option_ids(terms: dict[str, Any]) -> list[str]:
    return [terms["full_balance"]["option_id"], terms["payment_plans"]["option_id"]] + [
        d["option_id"] for d in terms["discounts"]
    ]


def _option(terms: dict[str, Any], option_id: str) -> tuple[str, dict[str, Any]]:
    """The option the policy lists under ``option_id``, and its kind: pay_in_full, plan or discount."""
    if option_id == terms["full_balance"]["option_id"]:
        return "pay_in_full", terms["full_balance"]
    if option_id == terms["payment_plans"]["option_id"]:
        return "plan", terms["payment_plans"]
    for discount in terms["discounts"]:
        if discount["option_id"] == option_id:
            return "discount", discount
    raise ToolError(
        f"{option_id} is not an option on this account's policy: {', '.join(_option_ids(terms))}."
    )


def _method(session: RemiCall, method_id: str) -> SavedMethod:
    saved = session.payment_methods.get(method_id)
    if saved is None:
        raise ToolError(
            f"Unknown method_id {method_id}: no card or bank account with that id was saved on this call."
        )
    return saved


class PaymentArgs(BaseModel):
    method_id: str = Field(description="The method_id add_payment_method returned.")
    option_id: str = Field(description=f"{OPTION_ID} 'payment_plan' charges the plan's down payment.")


@REGISTRY.tool(PaymentArgs)
async def take_payment(session: RemiCall, args: PaymentArgs) -> dict[str, Any]:
    """Charge a saved payment method now on PayNearMe for one of the account's options: the full balance ('pif'),
    a discount ('discount_<pct>'), or a plan's down payment ('payment_plan'). Returns PayNearMe's answer."""
    account = await session.ensure_account()
    saved = _method(session, args.method_id)
    kind, option = _option(await session.read_policy(), args.option_id)
    if kind == "plan":
        if not option["down_payment"]:
            raise ToolError(
                "This account's payment plans have no down payment: nothing to charge for payment_plan."
            )
        amount = policy.cents(option["down_payment"]["amount"])
    else:
        amount = policy.cents(option["amount"])
    async with session.charge_lock:
        try:
            payment = await session.payments.pay(
                order=account.pnm_order_identifier, method=saved, amount_cents=amount, at=session.clock.now()
            )
        except PaymentsUnavailable as err:
            logger.error(
                "PayNearMe unavailable charging %s on %s: %s", args.option_id, account.account_id, err
            )
            raise ToolError(
                "PayNearMe did not answer: whether the payment went through is not known."
            ) from err
        await _record_payment(session, payment)
    posted = payment.approved and await _post(session, payment, write_off=kind == "discount")
    result: dict[str, Any] = {
        "payment_id": payment.payment_id,
        "status": payment.status,
        "amount": policy.dollars(payment.amount_cents),
        "response_code": payment.response_code,
        "response_message": payment.response_message,
        "paid_with": _described(saved),
        "posted_to_ledger": posted,
    }
    if posted:
        # The ledger took the posting but may not answer the read: read_debt logs that, and the balance is
        # then simply not in the result (it is not known now).
        with contextlib.suppress(ToolError):
            result["remaining_balance"] = policy.dollars((await session.read_debt()).balance_cents)
    if payment.approved:
        session.outcomes.add({"pay_in_full": "paid", "discount": "settled"}.get(kind, "down_payment"))
    return result


async def _record_payment(session: RemiCall, payment: Payment) -> None:
    """The agency's record of PayNearMe's payment (World v2 ``payments``), approved or not."""
    async with session.db.transaction() as tx:
        await tx.insert(
            "payments",
            {
                "payment_id": payment.payment_id,
                "account_id": session.context.account_id,
                "method_id": payment.method_id,
                "amount_cents": payment.amount_cents,
                "status": payment.status,
                "response_code": payment.response_code,
                "response_message": payment.response_message,
                "payment_type": payment.kind,
                "last4": payment.last4,
                "at": session.clock.now(),
            },
        )


async def _post(session: RemiCall, payment: Payment, *, write_off: bool) -> bool:
    """Post an approved payment to the account's loan, keyed by PayNearMe's payment id, then, for a discount,
    write off the rest. Tried twice while the ledger is out of reach; False (and logged) when it stayed out of
    reach or refused: the money was taken and a person must post it."""
    ledger, account_id, today = session.ledger, session.context.account_id, session.clock.today()
    for attempt in (1, 2):
        try:
            debt = await ledger.debt(account_id)
            await ledger.post_payment(
                debt,
                amount_cents=payment.amount_cents,
                on=today,
                kind=payment.kind,
                payment_id=payment.payment_id,
            )
            if write_off:
                await ledger.write_off(
                    debt,
                    on=today,
                    external_id=f"wo_{payment.payment_id}",
                    note=f"Discount paid: PayNearMe payment {payment.payment_id}, {policy.dollars(payment.amount_cents)}.",
                )
            return True
        except LedgerUnavailable as err:
            logger.error(
                "posting %s to %s failed (attempt %d): %s", payment.payment_id, account_id, attempt, err
            )
        except LedgerRefused as err:
            logger.error(
                "the ledger refused posting %s to %s: %s (%s)", payment.payment_id, account_id, err, err.code
            )
            return False
    return False


class ArrangementArgs(BaseModel):
    option_id: str = Field(description=f"{OPTION_ID} The option the consumer agreed to.")
    months: int | None = Field(
        None, ge=1, description="payment_plan: the number of monthly payments the consumer agreed to."
    )
    first_due_on: date = Field(
        description="YYYY-MM-DD. payment_plan: the first monthly payment's date (later ones fall one in each month "
        "after it). A discount or the full balance: the date it is paid by."
    )


@REGISTRY.tool(ArrangementArgs)
async def create_arrangement(session: RemiCall, args: ArrangementArgs) -> dict[str, Any]:
    """Record an agreement the consumer made: a payment plan of the months they agreed to (each payment is
    computed from what the plan covers: equal, rounded down to the cent, the last taking the remainder; the
    consumer pays each one, so no payment method is attached), or a discount or the full balance as one payment
    on a date. Queues the agreement's written confirmation."""
    terms = await session.read_policy()
    kind, option = _option(terms, args.option_id)
    if kind == "plan":
        if args.months is None:
            raise ToolError(
                "A payment plan needs months: the number of monthly payments the consumer agreed to."
            )
        total = policy.cents(option["plan_covers"])
        amounts = policy.plan_instalment_cents(total, args.months)
        dates = policy.monthly_due_dates(args.first_due_on, args.months)
        row_kind = "plan"
    else:
        total = policy.cents(option["amount"])
        amounts, dates = [total], [args.first_due_on]
        row_kind = "settlement_lump" if kind == "discount" else "pay_in_full"
    instalments = [{"due_on": d.isoformat(), "amount_cents": a} for d, a in zip(dates, amounts, strict=True)]
    arrangement_id = new_id("arr")
    due_by = policy.confirmation_letter_due_by(session.clock.today())
    async with session.db.transaction() as tx:
        await tx.insert(
            "arrangements",
            {
                "arrangement_id": arrangement_id,
                "account_id": session.context.account_id,
                "kind": row_kind,
                "total_cents": total,
                "instalments": instalments,
                "method_id": None,
                "created_at": session.clock.now(),
                "confirmation_letter_due_by": due_by,
            },
        )
        await session.queue_letters(tx, "plan_confirmation")
    if row_kind == "plan":
        session.outcomes.add("plan")
    return {
        "recorded": row_kind,
        "arrangement_id": arrangement_id,
        "total": policy.dollars(total),
        "instalments": [
            {"due_on": i["due_on"], "amount": policy.dollars(i["amount_cents"])} for i in instalments
        ],
        "payment_method": None,
        "confirmation_letter_due_by": due_by.isoformat(),
    }


class PromiseArgs(BaseModel):
    option_id: str = Field(description=f"{OPTION_ID} The full balance or a discount.")
    due_on: date = Field(description="The exact date the consumer will pay by, YYYY-MM-DD.")
    method: Literal["card", "ach", "check"]


@REGISTRY.tool(PromiseArgs)
async def record_promise(session: RemiCall, args: PromiseArgs) -> dict[str, Any]:
    """Record a promise to pay one of the account's options (the full balance or a discount) on an exact date, and
    how the consumer will pay."""
    kind, option = _option(await session.read_policy(), args.option_id)
    if kind == "plan":
        raise ToolError("payment_plan is several payments, not one amount: a plan is create_arrangement.")
    amount = policy.cents(option["amount"])
    async with session.db.transaction() as tx:
        await tx.insert(
            "promises_to_pay",
            {
                "promise_id": new_id("ptp"),
                "account_id": session.context.account_id,
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
