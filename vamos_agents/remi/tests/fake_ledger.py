"""An in-memory ledger for Remi's tool tests, keeping the behaviour of the World's Fineract that Remi relies on.

Each rule here was measured on the pinned Fineract engine (the benchmark repo
``tests/test_collections_fineract.py`` runs the same operations live, and
``test_ledger_live.py`` runs ``FineractLedger`` itself against one):

- a transaction dated after the business date is refused
  (``error.msg.loan.transaction.cannot.be.a.future.date``);
- a second transaction with the same ``externalId`` is refused
  (``validation.msg.loan.transaction.externalId.value.must.be.unique``);
- a repayment of the whole outstanding balance closes the loan (600); a write-off
  closes it written off (601);
- a re-age spreads the outstanding balance into monthly instalments, each rounded
  half up to the cent, the last absorbing the difference; one a day per loan
  (``error.msg.loan.reage.reage.transaction.already.present.for.today``) unless
  the earlier one is undone.

It records every write in ``writes`` (the operation and its arguments), as the
World's ledger log would show them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from vamos_agents.remi import policy
from vamos_agents.remi.ledger import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_OVERPAID,
    STATUS_WRITTEN_OFF,
    Debt,
    Instalment,
    Itemization,
    LedgerRefused,
    LedgerUnavailable,
    Posting,
)

BUSINESS_DATE = date(2027, 2, 10)


@dataclass
class _Loan:
    account_id: str
    loan_id: int
    itemization: Itemization
    placed_cents: int
    repayments: list[Posting] = field(default_factory=list)
    status: int = STATUS_ACTIVE
    written_off_cents: int = 0
    schedule: tuple[Instalment, ...] = ()
    re_aged_on: date | None = None
    external_ids: set[str] = field(default_factory=set)

    @property
    def paid_cents(self) -> int:
        return sum(p.amount_cents for p in self.repayments)

    @property
    def balance_cents(self) -> int:
        if self.status == STATUS_WRITTEN_OFF:
            return 0
        return max(0, self.placed_cents - self.paid_cents)


class FakeLedger:
    """The World's ledger for one test: accounts are added with ``add_account``."""

    def __init__(self, business_date: date = BUSINESS_DATE) -> None:
        self.business_date: date | None = business_date
        self.payment_types = {"card": 2, "ach": 3}
        self.loans: dict[str, _Loan] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.unavailable = False  # every call raises LedgerUnavailable while set
        self.closed = False

    def add_account(
        self,
        account_id: str,
        *,
        charge_off_on: date,
        charge_off_balance_cents: int,
        interest_since_charge_off_cents: int = 0,
        fees_since_charge_off_cents: int = 0,
        paid_since_charge_off_cents: int = 0,
        last_payment_on: date | None = None,
    ) -> None:
        item = Itemization(
            charge_off_on,
            charge_off_balance_cents,
            interest_since_charge_off_cents,
            fees_since_charge_off_cents,
        )
        loan = _Loan(
            account_id,
            len(self.loans) + 1,
            item,
            charge_off_balance_cents + interest_since_charge_off_cents + fees_since_charge_off_cents,
        )
        if paid_since_charge_off_cents:
            assert last_payment_on is not None
            loan.repayments.append(
                Posting(0, f"seed_{account_id}", paid_since_charge_off_cents, last_payment_on)
            )
        self.loans[account_id] = loan

    # --- the Ledger protocol ------------------------------------------------------------------

    async def connect(self) -> None:
        self._reachable()

    async def close(self) -> None:
        self.closed = True

    async def debt(self, account_id: str) -> Debt:
        self._reachable()
        loan = self.loans.get(account_id)
        if loan is None:
            raise LedgerRefused("error.msg.loan.external.id.not.found", f"no loan {account_id}")
        return Debt(
            account_id=account_id,
            loan_id=loan.loan_id,
            status=loan.status,
            itemization=loan.itemization,
            paid_cents=loan.paid_cents,
            balance_cents=loan.balance_cents,
            last_payment_on=max((p.on for p in loan.repayments), default=None),
            schedule=loan.schedule,
        )

    async def find_posting(self, account_id: str, payment_id: str) -> Posting | None:
        self._reachable()
        return next((p for p in self.loans[account_id].repayments if p.payment_id == payment_id), None)

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
        self._reachable()
        loan = self._loan(debt)
        self._dated(on)
        self._unique(loan, payment_id)
        posting = Posting(len(self.writes) + 1, payment_id, amount_cents, on)
        loan.repayments.append(posting)
        if loan.balance_cents == 0:
            loan.status = STATUS_OVERPAID if loan.paid_cents > loan.placed_cents else STATUS_CLOSED
        self.writes.append(
            (
                "repayment",
                {
                    "amount_cents": amount_cents,
                    "on": on,
                    "kind": kind,
                    "payment_id": payment_id,
                    "receipt": processor_ref,
                },
            )
        )
        return posting

    async def write_off(self, debt: Debt, *, on: date, external_id: str, note: str) -> None:
        self._reachable()
        loan = self._loan(debt)
        self._dated(on)
        self._unique(loan, external_id)
        if loan.status != STATUS_ACTIVE:
            raise LedgerRefused("error.msg.loan.is.closed", "the loan is not active")
        loan.written_off_cents = loan.balance_cents
        loan.status = STATUS_WRITTEN_OFF
        self.writes.append(
            ("writeoff", {"on": on, "external_id": external_id, "amount_cents": loan.written_off_cents})
        )

    async def re_age(self, debt: Debt, *, instalments: int, first_due_on: date, external_id: str) -> Debt:
        self._reachable()
        loan = self._loan(debt)
        if loan.re_aged_on == self.business_date:
            raise LedgerRefused(
                "error.msg.loan.reage.reage.transaction.already.present.for.today",
                "Loan reaging can only be done once a day. There has already been a reaging done for today",
            )
        self._unique(loan, external_id)
        amounts = policy.instalment_cents(loan.balance_cents, instalments)
        loan.schedule = tuple(
            Instalment(policy.add_months(first_due_on, i), amount, 0) for i, amount in enumerate(amounts)
        )
        loan.re_aged_on = self.business_date
        self.writes.append(
            ("reAge", {"instalments": instalments, "first_due_on": first_due_on, "external_id": external_id})
        )
        return await self.debt(debt.account_id)

    async def undo_re_age(self, debt: Debt) -> None:
        self._reachable()
        loan = self._loan(debt)
        loan.schedule, loan.re_aged_on = (), None
        self.writes.append(("undoReAge", {}))

    # --- helpers ------------------------------------------------------------------------------

    def _reachable(self) -> None:
        if self.unavailable:
            raise LedgerUnavailable("the ledger could not be reached: connection refused")

    def _loan(self, debt: Debt) -> _Loan:
        return self.loans[debt.account_id]

    def _dated(self, on: date) -> None:
        assert self.business_date is not None
        if on > self.business_date:
            raise LedgerRefused(
                "error.msg.loan.transaction.cannot.be.a.future.date", "the date cannot be in the future"
            )

    @staticmethod
    def _unique(loan: _Loan, external_id: str) -> None:
        if external_id in loan.external_ids:
            raise LedgerRefused(
                "validation.msg.loan.transaction.externalId.value.must.be.unique",
                "the external id must be unique",
            )
        loan.external_ids.add(external_id)

    def balance(self, account_id: str) -> int:
        return self.loans[account_id].balance_cents

    def status(self, account_id: str) -> int:
        return self.loans[account_id].status
