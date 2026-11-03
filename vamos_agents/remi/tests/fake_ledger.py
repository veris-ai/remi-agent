"""An in-memory ledger for Remi's tool tests, keeping the behaviour of World v2's Fineract that Remi relies on.

Each rule here was measured on the pinned Fineract engine (the benchmark repo's
``tests/test_collections_world_v2.py`` runs the same operations live, and
``test_ledger_live.py`` runs ``FineractLedger`` itself against one):

- a transaction dated after the business date (3 November 2026) is refused
  (``error.msg.loan.transaction.cannot.be.a.future.date``);
- a second transaction with the same ``externalId`` is refused
  (``validation.msg.loan.transaction.externalId.value.must.be.unique``);
- a repayment of the whole outstanding balance closes the loan (600); a write-off
  closes it written off (601), owing nothing.

Nothing re-ages a loan: a plan is the agency's ``arrangements`` row only. Every
write is recorded in ``writes`` (the operation and its arguments), as the World's
ledger log would show it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from vamos_agents.remi.ledger import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_OVERPAID,
    STATUS_WRITTEN_OFF,
    Debt,
    Itemization,
    LedgerRefused,
    LedgerUnavailable,
    Posting,
)

BUSINESS_DATE = date(2026, 11, 3)


@dataclass
class _Loan:
    account_id: str
    loan_id: int
    itemization: Itemization
    principal_cents: int
    repayments: list[Posting] = field(default_factory=list)
    status: int = STATUS_ACTIVE
    written_off_cents: int = 0
    external_ids: set[str] = field(default_factory=set)

    @property
    def paid_cents(self) -> int:
        return sum(p.amount_cents for p in self.repayments)

    @property
    def balance_cents(self) -> int:
        if self.status == STATUS_WRITTEN_OFF:
            return 0
        return max(0, self.principal_cents - self.paid_cents)


class FakeLedger:
    """World v2's ledger for one test: one loan per placed account."""

    def __init__(self, business_date: date = BUSINESS_DATE) -> None:
        self.business_date: date | None = business_date
        self.payment_types = {"card": 2, "ach": 3}
        self.loans: dict[str, _Loan] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.unavailable = False  # every call raises LedgerUnavailable while set
        self.closed = False

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> FakeLedger:
        """Every loan the World's manifest places (``fineract.loans``)."""
        ledger = cls(date.fromisoformat(manifest["fineract"]["business_date"]))
        for loan in manifest["fineract"]["loans"]:
            item = loan["itemization"]
            ledger.add_account(
                loan["external_id"],
                Itemization(
                    item["itemized_from"],
                    date.fromisoformat(item["itemized_on"]),
                    item["itemized_balance_cents"],
                    item["interest_since_cents"],
                    item["fees_since_cents"],
                ),
                loan["principal_cents"],
            )
            assert not loan["repayments"], "World v2 seeds no payment since placement"
        return ledger

    def add_account(self, account_id: str, itemization: Itemization, principal_cents: int) -> None:
        self.loans[account_id] = _Loan(account_id, len(self.loans) + 1, itemization, principal_cents)

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
        )

    async def find_posting(self, account_id: str, payment_id: str) -> Posting | None:
        self._reachable()
        return next((p for p in self.loans[account_id].repayments if p.payment_id == payment_id), None)

    async def post_payment(
        self, debt: Debt, *, amount_cents: int, on: date, kind: Literal["card", "ach"], payment_id: str
    ) -> Posting:
        self._reachable()
        loan = self._loan(debt)
        self._dated(on)
        self._unique(loan, payment_id)
        posting = Posting(len(self.writes) + 1, payment_id, amount_cents, on)
        loan.repayments.append(posting)
        if loan.balance_cents == 0:
            loan.status = STATUS_OVERPAID if loan.paid_cents > loan.principal_cents else STATUS_CLOSED
        self.writes.append(
            ("repayment", {"amount_cents": amount_cents, "on": on, "kind": kind, "payment_id": payment_id})
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
