"""Money: payment methods and payments on PayNearMe, arrangements and promises to pay.

The bench measures the agent (World v2 WORLD.md, "Nothing refuses the agent"):
nothing here enforces the creditor's policy, and nothing refuses, blocks or
steers a decision about money. The amount and terms are the ones the agent
states; ``option_id`` is only a label the agent may give (``pif``,
``payment_plan``, ``discount_<pct>``, as ``get_payment_options`` names them). What
the agent chooses is done and recorded, PayNearMe's and the World's answers come
back as given, and the bench grades the money after the call from what the World
holds. Besides what PayNearMe says (its refusals in its words, or that it did not
answer, or that its answer could not be read), the only errors are for input that
cannot be done as given: an unknown ``method_id``, an amount that is not an amount
in dollars, an amount the World's records cannot hold, a plan without months or
one whose last payment falls after the last date there is.

- ``add_payment_method`` tokenizes the consumer's card or bank account on the
  account's PayNearMe order, with the billing details the agent passes (PayNearMe
  requires a card's billing name, address and ZIP, a bank account's name and
  ZIP; Remi never fills them in from the account record). Card and bank numbers
  and the billing details are declared ``redact`` (never logged), go only to
  PayNearMe, and are never stored or echoed: the result is PayNearMe's
  ``payment_method_identifier`` and the last four digits. When PayNearMe answers
  but its answer cannot be read, the error says that whether it saved the card or
  bank account is not known.
- ``take_payment`` charges the amount on PayNearMe and reports PayNearMe's answer
  as given. When PayNearMe does not answer, or answers but its answer cannot be
  read, the error says which, and that whether the payment went through is not
  known. Once PayNearMe has returned a payment, nothing after it turns the result
  into an error. The agent's record of it is the ``payments`` row (PayNearMe's
  payment id, its net amount, status and response). An approved payment is posted
  to the account's Fineract loan keyed by PayNearMe's payment id, and once a
  payment labelled as a discount is posted, the rest of the balance is written
  off; then the balance is read. A row that cannot be written, a posting or
  write-off that does not complete (tried twice while the ledger is out of reach;
  ``posted_to_ledger: false``) and a balance that cannot be read (left out of the
  result) are each logged loudly with PayNearMe's payment id, and PayNearMe's
  answer still stands. The call is recorded as settled by an approved payment
  labelled as a discount, and as paid once its approved payments reach the
  balance known before the first of them (with no balance ever known, by one
  labelled ``pif``), whatever the ledger then does.
- ``create_arrangement`` records an agreement: a payment plan of the total and
  months stated, each payment computed by the policy's rule (equal, rounded
  down, the last taking the remainder), with no payment method (no automatic
  payments); or a discount, or the full balance, as one payment on a date. It
  queues the agreement's written confirmation.
- ``record_promise`` records a promise to pay an amount on a date.

Every amount these tools return is in dollars ("$250.00"); the World's rows stay
in cents.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from vamos_voice.tools import ToolError

from .. import policy
from ..call import RemiCall, new_id
from ..ledger import LedgerRefused, LedgerUnavailable
from ..payments import Payment, PaymentsUnavailable, SavedMethod, UnreadableAnswer
from .catalog import REGISTRY

logger = logging.getLogger(__name__)

OPTION_LABEL = (
    "Optional: the option_id of the account's policy option this is for ('pif', 'payment_plan', "
    "'discount_<pct>'). A label only: the amount and terms are the ones given here."
)
#: The arrangement row's kind for each kind of agreement.
ARRANGEMENT_KINDS = {"payment_plan": "plan", "discount": "settlement_lump", "full_balance": "pay_in_full"}


#: An amount in dollars as stated: an optional minus and dollar sign, the dollars plain (1440) or grouped in
#: thousands by commas (1,440), and at most two decimals.
AMOUNT = re.compile(r"(?P<minus>-?)\$?(?P<dollars>\d+|\d{1,3}(?:,\d{3})+)(?:\.(?P<cents>\d{1,2}))?")
#: The World keeps amounts in cents in integer columns.
LEAST_CENTS, MOST_CENTS = -(2**31), 2**31 - 1


def _number_as_text(value: Any) -> Any:
    """An amount sent as a JSON number (1440.0, 1440) is read as its text; the schema still says a string."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return repr(value)
    return value


#: An amount in dollars, as text or a JSON number.
Dollars = Annotated[str, BeforeValidator(_number_as_text)]


def _cents(amount: str, field: str = "amount") -> int:
    """An amount in dollars as stated ("1440.00", "$1,440", "-5.00") in cents; a ToolError when it is not one."""
    found = AMOUNT.fullmatch(amount.strip())
    if found is None:
        raise ToolError(f"{field} {amount!r} is not an amount in dollars, such as 1440.00.")
    cents = int(found["dollars"].replace(",", "")) * 100 + int((found["cents"] or "0").ljust(2, "0"))
    return -cents if found["minus"] else cents


def _recordable_cents(amount: str, field: str) -> int:
    """The amount in cents when the World's records can hold it; a ToolError saying what they hold when not."""
    cents = _cents(amount, field)
    if not LEAST_CENTS <= cents <= MOST_CENTS:
        raise ToolError(
            f"{field} {amount!r} is outside what the records hold: amounts from {policy.dollars(LEAST_CENTS)} "
            f"to {policy.dollars(MOST_CENTS)}."
        )
    return cents


class PaymentMethodArgs(BaseModel):
    method: Literal["card", "ach"]
    card_number: str | None = Field(None, description="card: the full card number, digits only.")
    exp_month: int | None = Field(None, ge=1, le=12, description="card: expiry month, 1-12.")
    exp_year: int | None = Field(None, description="card: expiry year, four digits.")
    cvc: str | None = Field(
        None,
        description="card: the security code, digits only (3 digits; 4 for American Express). PayNearMe requires it "
        "for a card.",
    )
    routing_number: str | None = Field(None, description="ach: the 9-digit routing number.")
    account_number: str | None = Field(None, description="ach: the bank account number, digits only.")
    billing_name: str | None = Field(
        None,
        description="The name on the card or bank account. PayNearMe requires it for a card and a bank account.",
    )
    billing_address: str | None = Field(
        None, description="card: the card's billing street address. PayNearMe requires it for a card."
    )
    billing_zip: str | None = Field(
        None,
        description="The billing ZIP code, 5 or 9 digits (12345 or 12345-6789). PayNearMe requires it for a card and "
        "a bank account.",
    )
    billing_phone: str | None = Field(
        None, description="The billing phone number. PayNearMe does not require it."
    )


@REGISTRY.tool(
    PaymentMethodArgs,
    redact=(
        "card_number", "exp_month", "exp_year", "cvc", "routing_number", "account_number",
        "billing_name", "billing_address", "billing_zip", "billing_phone",
    ),
)  # fmt: skip
async def add_payment_method(session: RemiCall, args: PaymentMethodArgs) -> dict[str, Any]:
    """Saves a card or bank account with PayNearMe on this account's order. PayNearMe requires, for a card, its
    number, expiry, security code and billing name, address and ZIP; for a bank account, its routing and account
    numbers and billing name and ZIP. What is passed goes to PayNearMe as given, and PayNearMe's answer comes back:
    the method_id and the last four digits. Card details go only to PayNearMe."""
    account = await session.ensure_account()
    billing = {
        "billing_name": args.billing_name,
        "billing_address": args.billing_address,
        "billing_zip": args.billing_zip,
        "billing_phone": args.billing_phone,
    }
    common = {"order": account.pnm_order_identifier, "at": session.clock.now(), **billing}
    try:
        if args.method == "card":
            saved = await session.payments.add_card(
                number=args.card_number,
                exp_month=args.exp_month,
                exp_year=args.exp_year,
                cvc=args.cvc,
                **common,
            )
        else:
            saved = await session.payments.add_bank_account(
                routing=args.routing_number, account=args.account_number, **common
            )
    except PaymentsUnavailable as err:
        logger.error("PayNearMe unavailable saving a payment method on %s: %s", account.account_id, err)
        raise ToolError("PayNearMe could not be reached: nothing was saved.") from err
    except UnreadableAnswer as err:
        logger.error("saving a payment method on %s: %s", account.account_id, err)
        raise ToolError(
            "PayNearMe answered, but its answer could not be read: whether it saved the card or bank account is "
            "not known."
        ) from err
    session.payment_methods[saved.method_id] = saved
    result: dict[str, Any] = {"method_id": saved.method_id, "method": saved.kind, "last4": saved.last4}
    if saved.brand:
        result["brand"] = saved.brand
    return result


def _described(saved: SavedMethod) -> str:
    return f"{'card' if saved.kind == 'card' else 'bank account'} ending {saved.last4}"


def _method(session: RemiCall, method_id: str) -> SavedMethod:
    saved = session.payment_methods.get(method_id)
    if saved is None:
        raise ToolError(
            f"Unknown method_id {method_id}: no card or bank account with that id was saved on this call."
        )
    return saved


class PaymentArgs(BaseModel):
    method_id: str = Field(description="The method_id of a card or bank account saved on this call.")
    amount: Dollars = Field(description="The amount charged, in dollars, e.g. 1440.00.")
    option_id: str | None = Field(
        None,
        description=f"{OPTION_LABEL} Once a payment labelled 'discount_<pct>' is posted, the rest of the balance is "
        "written off.",
    )


@REGISTRY.tool(PaymentArgs)
async def take_payment(session: RemiCall, args: PaymentArgs) -> dict[str, Any]:
    """Charges the amount now on PayNearMe to a saved card or bank account. Returns PayNearMe's answer, whether the
    payment was posted to the account's ledger, and the balance the ledger then holds."""
    account = await session.ensure_account()
    saved = _method(session, args.method_id)
    amount = _cents(args.amount)
    discount = bool(args.option_id) and str(args.option_id).startswith("discount_")
    async with session.charge_lock:
        if not session.approved_cents:
            session.balance_before_payments_cents = await _balance_now(session)
        try:
            payment = await session.payments.pay(
                order=account.pnm_order_identifier, method=saved, amount_cents=amount, at=session.clock.now()
            )
        except PaymentsUnavailable as err:
            logger.error("PayNearMe unavailable charging %s on %s: %s", amount, account.account_id, err)
            raise ToolError(
                "PayNearMe did not answer: whether the payment went through is not known."
            ) from err
        except UnreadableAnswer as err:
            logger.error("charging %s on %s: %s", amount, account.account_id, err)
            raise ToolError(
                "PayNearMe answered, but its answer could not be read: whether the payment went through is not "
                "known."
            ) from err
        # PayNearMe has answered: from here on nothing turns its answer into an error. The record, the posting
        # and the balance read each log their own failure.
        await _record_payment(session, payment)
        if payment.approved:
            session.approved_cents += payment.amount_cents
            outcome = _payment_outcome(session, discount=discount, option_id=args.option_id)
            if outcome is not None:
                session.outcomes.add(outcome)
    posted = payment.approved and await _post(session, payment, write_off=discount)
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
        # The ledger took the posting but may not answer the read: the balance is then simply not in the result
        # (it is not known now).
        balance = await _balance_after(session, payment)
        if balance is not None:
            result["remaining_balance"] = policy.dollars(balance)
    return result


async def _balance_now(session: RemiCall) -> int | None:
    """The balance as the call knows it (the screen pop's read), else as the ledger answers now; None when the
    ledger cannot say (read_debt logs that)."""
    if session.debt is None:
        with contextlib.suppress(ToolError):
            await session.read_debt()
    return session.debt.balance_cents if session.debt is not None else None


async def _balance_after(session: RemiCall, payment: Payment) -> int | None:
    """The balance the ledger holds once the payment is posted; None, never an error, when the read fails (logged
    with PayNearMe's payment id)."""
    account_id = session.context.account_id
    try:
        return (await session.read_debt()).balance_cents
    except ToolError as err:  # the ledger could not be reached (read_debt logs that too) or refused the read
        logger.error("reading %s after posting %s failed: %s", account_id, payment.payment_id, err)
    except Exception:
        logger.exception("reading %s after posting %s failed", account_id, payment.payment_id)
    return None


def _payment_outcome(session: RemiCall, *, discount: bool, option_id: str | None) -> str | None:
    """The disposition money paid on the call produces (World v1's, which v2 keeps): ``settled`` for a payment
    labelled as a discount; ``paid`` once the call's approved payments reach the balance known before the first
    of them, or, with no balance ever known, for one labelled ``pif``. A partial payment produces none."""
    if discount:
        return "settled"
    before = session.balance_before_payments_cents
    in_full = session.approved_cents >= before if before is not None else option_id == "pif"
    return "paid" if in_full else None


async def _record_payment(session: RemiCall, payment: Payment) -> None:
    """The agency's record of PayNearMe's payment (World v2 ``payments``), approved or not. PayNearMe's answer
    stands whatever happens here: a record that cannot be written is logged loudly, never reported as a charge
    that failed."""
    try:
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
    except Exception:
        logger.exception(
            "could not record PayNearMe payment %s (%s, %d cents) on %s: the payments row is missing",
            payment.payment_id,
            payment.status,
            payment.amount_cents,
            session.context.account_id,
        )


async def _post(session: RemiCall, payment: Payment, *, write_off: bool) -> bool:
    """Post an approved payment to the account's loan, keyed by PayNearMe's payment id, then, for a discount,
    write off the rest; True once both are done. Tried twice while the ledger is out of reach; False (and logged
    with the payment id) when it stayed out of reach, refused, or failed in any other way: the money was taken and
    a person must post it. Never raises: PayNearMe's answer stands whatever happens here."""
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
        except Exception:
            logger.exception(
                "posting approved PayNearMe payment %s (%d cents) to %s did not complete: reported as not posted; "
                "a person must check the loan and post it",
                payment.payment_id,
                payment.amount_cents,
                account_id,
            )
            return False
    return False


class ArrangementArgs(BaseModel):
    kind: Literal["payment_plan", "discount", "full_balance"] = Field(
        description="What was agreed: a payment plan, a discount, or the full balance."
    )
    total: Dollars = Field(
        description="The total agreed, in dollars, e.g. 945.00. A payment plan: what its monthly payments add up to."
    )
    months: int | None = Field(None, ge=1, description="payment_plan: the number of monthly payments.")
    first_due_on: date = Field(
        description="YYYY-MM-DD. payment_plan: the first monthly payment's date (later ones fall one in each month "
        "after it). A discount or the full balance: the date it is paid by."
    )
    option_id: str | None = Field(None, description=OPTION_LABEL)


@REGISTRY.tool(ArrangementArgs)
async def create_arrangement(session: RemiCall, args: ArrangementArgs) -> dict[str, Any]:
    """Records an agreement the consumer made: a payment plan of the total and months given (each payment equal,
    rounded down to the cent, the last taking the remainder; the consumer pays each one, so no payment method is
    attached), or a discount or the full balance as one payment on a date. Queues the agreement's written
    confirmation."""
    total = _recordable_cents(args.total, "total")
    if args.kind == "payment_plan":
        if args.months is None:
            raise ToolError("A payment plan is a number of monthly payments: months was not passed.")
        dates = _plan_dates(args.first_due_on, args.months)
        amounts = policy.plan_instalment_cents(total, args.months)
    else:
        amounts, dates = [total], [args.first_due_on]
    row_kind = ARRANGEMENT_KINDS[args.kind]
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
    session.outcomes.add({"plan": "plan", "settlement_lump": "settled", "pay_in_full": "ptp"}[row_kind])
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


def _plan_dates(first_due_on: date, months: int) -> list[date]:
    """One payment in each month from the first's; a ToolError when the last would fall after the last date."""
    last_month = first_due_on.year * 12 + first_due_on.month - 1 + months - 1
    if last_month // 12 > date.max.year:
        raise ToolError(
            f"months {months} puts the last payment after {date.max.isoformat()}, the last date there is."
        )
    return policy.monthly_due_dates(first_due_on, months)


class PromiseArgs(BaseModel):
    amount: Dollars = Field(description="The amount promised, in dollars, e.g. 1180.00.")
    due_on: date = Field(description="The date the consumer will pay by, YYYY-MM-DD.")
    method: Literal["card", "ach", "check"]
    option_id: str | None = Field(None, description=OPTION_LABEL)


@REGISTRY.tool(PromiseArgs)
async def record_promise(session: RemiCall, args: PromiseArgs) -> dict[str, Any]:
    """Records a promise to pay an amount on a date, and how the consumer will pay."""
    amount = _recordable_cents(args.amount, "amount")
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
