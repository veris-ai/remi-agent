"""The agency's ledger: each placed account's money in the World's Fineract (WORLD.md, "Money: the Fineract ledger").

The collections Postgres holds the workflow (who was called, what was flagged,
what was agreed); Fineract holds the books. A placed account ``TAS-NNNNNN`` is the
Fineract loan whose ``externalId`` is ``TAS-NNNNNN``: its principal is the placed
balance, its ``tas_itemization`` datatable row the itemization, its repayments
the payments since charge-off, and its ``totalOutstanding`` the current balance.
``FineractLedger`` is Remi's one client for it:

- ``debt`` reads an account's money;
- ``post_payment`` posts a payment the gateway approved, carrying the gateway's
  ``payment_id`` as the transaction's ``externalId``, so a retried posting lands
  once (Fineract refuses a repeated external id, and the retry finds the first);
- ``write_off`` waives what a one-payment settlement left;
- ``re_age`` puts the balance on a monthly plan and answers the schedule
  Fineract computed; ``undo_re_age`` takes it back.

Amounts are cents everywhere in Remi and dollars only on this wire; dates go out
in the format each request declares and come back as ``[y, m, d]``. Every date
passed in is the call's (``CallClock``): Fineract's business date is the World's
day and it refuses a transaction after it. One ``httpx.AsyncClient`` per pod,
every request with a timeout. A refusal is ``LedgerRefused`` (a ``ToolError`` the
model can act on, with Fineract's own code); an unreachable or failing ledger is
``LedgerUnavailable``, which the tool decides about and never hides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

import httpx

from vamos_voice.tools import ToolError

logger = logging.getLogger(__name__)

API_PATH = "/fineract-provider/api/v1"
FORMAT = {"dateFormat": "dd MMMM yyyy", "locale": "en"}
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DATATABLE = "tas_itemization"
#: The payment types an approved charge is posted as, by the gateway's method kind.
PAYMENT_TYPE_NAMES = {"card": "Card", "ach": "ACH"}
STATUS_ACTIVE, STATUS_CLOSED, STATUS_WRITTEN_OFF, STATUS_OVERPAID = 300, 600, 601, 700


class LedgerRefused(ToolError):
    """Fineract refused the request; ``code`` is its own globalisation code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"The ledger refused it: {detail}")
        self.code = code


class LedgerUnavailable(RuntimeError):
    """Fineract could not be reached, or failed (5xx)."""


def fmt(on: date) -> str:
    """``dd MMMM yyyy`` with an unpadded day: "4 July 2026"."""
    return f"{on.day} {on.strftime('%B %Y')}"


def parse_date(value: Any) -> date | None:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return date(int(value[0]), int(value[1]), int(value[2]))
    return None


def to_cents(amount: Any) -> int:
    return int((Decimal(str(amount or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_dollars(cents: int) -> str:
    """Major units as Fineract takes them, exactly: 18550 -> "185.50"."""
    return f"{Decimal(cents) / 100:.2f}"


@dataclass(frozen=True)
class Itemization:
    charge_off_on: date
    charge_off_balance_cents: int
    interest_since_charge_off_cents: int
    fees_since_charge_off_cents: int


@dataclass(frozen=True)
class Instalment:
    due_on: date
    amount_cents: int
    paid_cents: int


@dataclass(frozen=True)
class Debt:
    """One account's money, as the ledger holds it right now."""

    account_id: str
    loan_id: int
    status: int
    itemization: Itemization
    paid_cents: int  # since charge-off: every repayment
    balance_cents: int  # what is outstanding
    last_payment_on: date | None
    schedule: tuple[Instalment, ...] = field(default=())

    @property
    def open(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass(frozen=True)
class Posting:
    transaction_id: int
    payment_id: str
    amount_cents: int
    on: date


def _error_code(body: Any) -> tuple[str, str]:
    """Fineract's most specific code and message from its error envelope."""
    if not isinstance(body, dict):
        return "unknown", str(body)[:200]
    errors = body.get("errors") or []
    first = errors[0] if errors else body
    code = first.get("userMessageGlobalisationCode") or body.get("userMessageGlobalisationCode") or "unknown"
    message = first.get("defaultUserMessage") or body.get("defaultUserMessage") or code
    return str(code), str(message)


class Ledger(Protocol):
    """What Remi asks of the ledger; ``FineractLedger`` is the World's (tests use an in-memory one)."""

    payment_types: dict[str, int]
    business_date: date | None

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def debt(self, account_id: str) -> Debt: ...
    async def find_posting(self, account_id: str, payment_id: str) -> Posting | None: ...
    async def post_payment(
        self,
        debt: Debt,
        *,
        amount_cents: int,
        on: date,
        kind: Literal["card", "ach"],
        payment_id: str,
        processor_ref: str,
    ) -> Posting: ...
    async def write_off(self, debt: Debt, *, on: date, external_id: str, note: str) -> None: ...
    async def re_age(self, debt: Debt, *, instalments: int, first_due_on: date, external_id: str) -> Debt: ...
    async def undo_re_age(self, debt: Debt) -> None: ...


class FineractLedger:
    """The World's Fineract over HTTP. Construct, then ``connect`` (it resolves the payment types)."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        tenant: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=base if base.endswith(API_PATH) else base + API_PATH,
            auth=(user, password),
            headers={"Fineract-Platform-TenantId": tenant},
            timeout=TIMEOUT,
            transport=transport,
        )
        self.payment_types: dict[str, int] = {}
        self.business_date: date | None = None

    async def connect(self) -> None:
        """Resolve the payment types by name and read the business date; raises if the ledger is not ready."""
        types = {t["name"]: int(t["id"]) for t in await self._call("GET", "/paymenttypes") or []}
        missing = [name for name in PAYMENT_TYPE_NAMES.values() if name not in types]
        if missing:
            raise LedgerUnavailable(f"the ledger has no payment type {missing}")
        self.payment_types = {kind: types[name] for kind, name in PAYMENT_TYPE_NAMES.items()}
        rows = await self._call("GET", "/businessdate")
        self.business_date = next(
            (parse_date(r["date"]) for r in rows if r.get("type") == "BUSINESS_DATE"), None
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, *, missing_ok: bool = False, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise LedgerUnavailable(f"the ledger could not be reached: {err}") from err
        if missing_ok and resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise LedgerUnavailable(f"the ledger failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            code, message = _error_code(body)
            raise LedgerRefused(code, message)
        return body

    # --- reads -----------------------------------------------------------------------------

    async def debt(self, account_id: str) -> Debt:
        loan = await self._call(
            "GET",
            f"/loans/external-id/{account_id}",
            params={"associations": "repaymentSchedule,transactions"},
        )
        rows = await self._call("GET", f"/datatables/{DATATABLE}/{loan['id']}") or []
        if not rows:
            raise LedgerUnavailable(f"loan {account_id} has no {DATATABLE} row")
        return _debt(account_id, loan, rows[0])

    async def find_posting(self, account_id: str, payment_id: str) -> Posting | None:
        found = await self._call(
            "GET", f"/loans/external-id/{account_id}/transactions/external-id/{payment_id}", missing_ok=True
        )
        if not found or found.get("manuallyReversed"):
            return None
        return Posting(
            int(found["id"]), payment_id, to_cents(found["amount"]), parse_date(found["date"]) or date.min
        )

    # --- writes ----------------------------------------------------------------------------

    async def post_payment(
        self,
        debt: Debt,
        *,
        amount_cents: int,
        on: date,
        kind: Literal["card", "ach"],
        payment_id: str,
        processor_ref: str,
    ) -> Posting:
        """A repayment of an approved charge, once: a retry after a lost answer finds the first posting."""
        body = {
            "transactionDate": fmt(on),
            "transactionAmount": to_dollars(amount_cents),
            "paymentTypeId": self.payment_types[kind],
            "externalId": payment_id,
            "receiptNumber": processor_ref,
            "note": f"Gateway payment {payment_id}",
            **FORMAT,
        }
        try:
            result = await self._call(
                "POST", f"/loans/{debt.loan_id}/transactions", params={"command": "repayment"}, json=body
            )
        except LedgerRefused as err:
            if "externalId" not in err.code:
                raise
            existing = await self.find_posting(debt.account_id, payment_id)
            if existing is None:
                raise
            return existing
        return Posting(int(result["resourceId"]), payment_id, amount_cents, on)

    async def write_off(self, debt: Debt, *, on: date, external_id: str, note: str) -> None:
        await self._call(
            "POST",
            f"/loans/{debt.loan_id}/transactions",
            params={"command": "writeoff"},
            json={"transactionDate": fmt(on), "externalId": external_id, "note": note, **FORMAT},
        )

    async def re_age(self, debt: Debt, *, instalments: int, first_due_on: date, external_id: str) -> Debt:
        """Spread the balance into ``instalments`` monthly payments from ``first_due_on``; the debt re-read."""
        await self._call(
            "POST",
            f"/loans/{debt.loan_id}/transactions",
            params={"command": "reAge"},
            json={
                "frequencyNumber": 1,
                "frequencyType": "MONTHS",
                "startDate": fmt(first_due_on),
                "numberOfInstallments": instalments,
                "externalId": external_id,
                **FORMAT,
            },
        )
        return await self.debt(debt.account_id)

    async def undo_re_age(self, debt: Debt) -> None:
        await self._call(
            "POST", f"/loans/{debt.loan_id}/transactions", params={"command": "undoReAge"}, json=dict(FORMAT)
        )


def _debt(account_id: str, loan: dict[str, Any], item: dict[str, Any]) -> Debt:
    summary = loan.get("summary") or {}
    repayments = [
        t
        for t in loan.get("transactions") or []
        if (t.get("type") or {}).get("repayment") and not t.get("manuallyReversed")
    ]
    periods = (loan.get("repaymentSchedule") or {}).get("periods") or []
    schedule = tuple(
        Instalment(
            parse_date(p["dueDate"]) or date.min,
            to_cents(p.get("totalDueForPeriod")),
            to_cents(p.get("totalPaidForPeriod")),
        )
        for p in periods
        if p.get("period") and to_cents(p.get("totalDueForPeriod")) > 0
    )
    charge_off_on = parse_date(item.get("charge_off_on"))
    if charge_off_on is None:
        raise LedgerUnavailable(f"loan {account_id}: no charge-off date in its itemization")
    return Debt(
        account_id=account_id,
        loan_id=int(loan["id"]),
        status=int((loan.get("status") or {}).get("id") or 0),
        itemization=Itemization(
            charge_off_on,
            to_cents(item.get("charge_off_balance")),
            to_cents(item.get("interest_since_charge_off")),
            to_cents(item.get("fees_since_charge_off")),
        ),
        paid_cents=to_cents(summary.get("totalRepayment")),
        balance_cents=to_cents(summary.get("totalOutstanding")),
        last_payment_on=max((parse_date(t["date"]) for t in repayments), default=None),
        schedule=schedule,
    )
