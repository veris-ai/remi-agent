"""``FineractLedger`` on the wire: what it sends, how it reads Fineract's answers, and how it fails.

Fineract is replaced by an ``httpx.MockTransport`` that answers with bodies
shaped as the pinned engine answers (recorded in utility-bench
``tests/test_collections_fineract.py`` and services-sandbox
``services/fineract/docs/fineract-measurements.md``). ``test_ledger_live`` runs
the same client against a real Fineract.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from tests.guards import assert_no_wall_clock
from vamos_agents.remi import ledger as ledger_module
from vamos_agents.remi.ledger import FineractLedger, LedgerRefused, LedgerUnavailable

API = "/fineract-provider/api/v1"
LOAN = {
    "id": 7,
    "externalId": "TAS-420103",
    "status": {"id": 300},
    "summary": {"principalDisbursed": 322.75, "totalRepayment": 10.0, "totalOutstanding": 312.75},
    "transactions": [
        {"type": {"disbursement": True}, "date": [2025, 3, 29], "amount": 322.75},
        {"type": {"repayment": True}, "date": [2025, 6, 27], "amount": 10.0, "externalId": "seed_TAS-420103"},
        {"type": {"repayment": True}, "date": [2025, 7, 1], "amount": 5.0, "manuallyReversed": True},
    ],
    "repaymentSchedule": {
        "periods": [
            {"dueDate": [2025, 3, 29], "principalDisbursed": 322.75},
            {"period": 1, "dueDate": [2025, 4, 29], "totalDueForPeriod": 10.0, "totalPaidForPeriod": 10.0},
            {"period": 2, "dueDate": [2027, 3, 1], "totalDueForPeriod": 52.13, "totalPaidForPeriod": 0},
            {"period": 3, "dueDate": [2027, 4, 1], "totalDueForPeriod": 0, "totalPaidForPeriod": 0},
        ]
    },
}
ITEMIZATION = [
    {
        "loan_id": 7,
        "charge_off_on": [2025, 3, 29],
        "charge_off_balance": 275.0,
        "interest_since_charge_off": 29.75,
        "fees_since_charge_off": 18.0,
    }
]


def _refusal(code: str, status: int = 403) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "httpStatusCode": str(status),
            "defaultUserMessage": "Errors contain reason for domain rule violation.",
            "userMessageGlobalisationCode": "validation.msg.domain.rule.violation",
            "errors": [{"userMessageGlobalisationCode": code, "defaultUserMessage": code.split(".")[-1]}],
        },
    )


class FakeFineract:
    """Answers the paths Remi uses; records each request as (method, path with query, body)."""

    def __init__(self, handle: Callable[[httpx.Request], httpx.Response | None] | None = None) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.handle = handle

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix(API)
        query = request.url.query.decode()
        self.requests.append((request.method, f"{path}?{query}" if query else path, body))
        assert request.headers["Fineract-Platform-TenantId"] == "default"
        assert request.headers["Authorization"].startswith("Basic ")
        if self.handle and (custom := self.handle(request)) is not None:
            return custom
        if path == "/paymenttypes":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "Money Transfer"},
                    {"id": 8, "name": "Card"},
                    {"id": 9, "name": "ACH"},
                ],
            )
        if path == "/businessdate":
            return httpx.Response(
                200,
                json=[
                    {"type": "BUSINESS_DATE", "date": [2027, 2, 10]},
                    {"type": "COB_DATE", "date": [2027, 2, 9]},
                ],
            )
        if path == "/loans/external-id/TAS-420103":
            return httpx.Response(200, json=LOAN)
        if path == "/datatables/tas_itemization/7":
            return httpx.Response(200, json=ITEMIZATION)
        if path == "/loans/7/transactions":
            return httpx.Response(200, json={"loanId": 7, "resourceId": 41})
        return httpx.Response(404, json={"userMessageGlobalisationCode": "error.msg.resource.not.found"})


async def _ledger(fake: FakeFineract) -> FineractLedger:
    ledger = FineractLedger(
        "https://twin.example", "mifos", "password", "default", transport=httpx.MockTransport(fake)
    )
    await ledger.connect()
    return ledger


async def test_connect_resolves_the_payment_types_and_the_business_date() -> None:
    ledger = await _ledger(FakeFineract())
    assert ledger.payment_types == {"card": 8, "ach": 9}
    assert ledger.business_date == date(2027, 2, 10)


async def test_a_ledger_without_the_payment_types_is_not_ready() -> None:
    fake = FakeFineract(
        lambda r: httpx.Response(200, json=[]) if r.url.path.endswith("/paymenttypes") else None
    )
    with pytest.raises(LedgerUnavailable, match="no payment type"):
        await _ledger(fake)


async def test_the_debt_is_the_loan_and_its_itemization_in_cents() -> None:
    debt = await (await _ledger(FakeFineract())).debt("TAS-420103")
    assert (debt.loan_id, debt.status, debt.open) == (7, 300, True)
    assert (debt.balance_cents, debt.paid_cents, debt.last_payment_on) == (31275, 1000, date(2025, 6, 27))
    item = debt.itemization
    assert (
        item.charge_off_on,
        item.charge_off_balance_cents,
        item.interest_since_charge_off_cents,
        item.fees_since_charge_off_cents,
    ) == (
        date(2025, 3, 29),
        27500,
        2975,
        1800,
    )
    assert [(i.due_on, i.amount_cents, i.paid_cents) for i in debt.schedule] == [
        (date(2025, 4, 29), 1000, 1000),
        (date(2027, 3, 1), 5213, 0),
    ]


async def test_a_posting_carries_the_call_date_dollars_the_method_and_the_gateways_ids() -> None:
    fake = FakeFineract()
    ledger = await _ledger(fake)
    debt = await ledger.debt("TAS-420103")
    posting = await ledger.post_payment(
        debt,
        amount_cents=31275,
        on=date(2027, 2, 10),
        kind="ach",
        payment_id="pay_abc",
        processor_ref="rp_xyz",
    )
    assert (posting.transaction_id, posting.amount_cents, posting.payment_id) == (41, 31275, "pay_abc")
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "/loans/7/transactions?command=repayment")
    assert body == {
        "transactionDate": "10 February 2027",
        "transactionAmount": "312.75",
        "paymentTypeId": 9,
        "externalId": "pay_abc",
        "receiptNumber": "rp_xyz",
        "note": "Gateway payment pay_abc",
        "dateFormat": "dd MMMM yyyy",
        "locale": "en",
    }


async def test_a_repeated_posting_finds_the_first_instead_of_posting_twice() -> None:
    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/transactions"):
            return _refusal("validation.msg.loan.transaction.externalId.value.must.be.unique", 400)
        if request.url.path.endswith("/transactions/external-id/pay_abc"):
            return httpx.Response(200, json={"id": 41, "amount": 312.75, "date": [2027, 2, 10]})
        return None

    ledger = await _ledger(FakeFineract(handle))
    debt = await ledger.debt("TAS-420103")
    posting = await ledger.post_payment(
        debt,
        amount_cents=31275,
        on=date(2027, 2, 10),
        kind="card",
        payment_id="pay_abc",
        processor_ref="rp_xyz",
    )
    assert (posting.transaction_id, posting.amount_cents, posting.on) == (41, 31275, date(2027, 2, 10))


async def test_a_refusal_is_fineracts_own_code_and_a_tool_error() -> None:
    fake = FakeFineract(
        lambda r: (
            _refusal("error.msg.loan.transaction.cannot.be.a.future.date") if r.method == "POST" else None
        )
    )
    ledger = await _ledger(fake)
    debt = await ledger.debt("TAS-420103")
    with pytest.raises(LedgerRefused) as refused:
        await ledger.post_payment(
            debt,
            amount_cents=100,
            on=date(2027, 2, 11),
            kind="card",
            payment_id="pay_x",
            processor_ref="rp_x",
        )
    assert refused.value.code == "error.msg.loan.transaction.cannot.be.a.future.date"
    assert str(refused.value).startswith("The ledger refused it: ")


@pytest.mark.parametrize(
    "fail",
    [
        lambda r: httpx.Response(503, json={"detail": "upstream service not ready"}),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("connection refused", request=r)),
    ],
    ids=["5xx", "unreachable"],
)
async def test_an_unreachable_or_failing_ledger_is_unavailable_not_a_refusal(fail) -> None:
    ledger = await _ledger(FakeFineract())
    ledger._http._transport = httpx.MockTransport(fail)
    with pytest.raises(LedgerUnavailable):
        await ledger.debt("TAS-420103")


async def test_write_off_and_re_age_send_the_commands_worldmd_documents() -> None:
    fake = FakeFineract()
    ledger = await _ledger(fake)
    debt = await ledger.debt("TAS-420103")
    await ledger.write_off(debt, on=date(2027, 2, 10), external_id="wo_pay_abc", note="Settled.")
    await ledger.re_age(debt, instalments=6, first_due_on=date(2027, 3, 1), external_id="arr_1")
    await ledger.undo_re_age(debt)
    writes = [(m, p, b) for m, p, b in fake.requests if m == "POST"]
    assert writes == [
        (
            "POST",
            "/loans/7/transactions?command=writeoff",
            {
                "transactionDate": "10 February 2027",
                "externalId": "wo_pay_abc",
                "note": "Settled.",
                "dateFormat": "dd MMMM yyyy",
                "locale": "en",
            },
        ),
        (
            "POST",
            "/loans/7/transactions?command=reAge",
            {
                "frequencyNumber": 1,
                "frequencyType": "MONTHS",
                "startDate": "1 March 2027",
                "numberOfInstallments": 6,
                "externalId": "arr_1",
                "dateFormat": "dd MMMM yyyy",
                "locale": "en",
            },
        ),
        ("POST", "/loans/7/transactions?command=undoReAge", {"dateFormat": "dd MMMM yyyy", "locale": "en"}),
    ]


def test_dates_and_amounts_cross_the_wire_exactly() -> None:
    assert ledger_module.fmt(date(2026, 7, 4)) == "4 July 2026"
    assert ledger_module.to_dollars(18550) == "185.50" and ledger_module.to_dollars(5) == "0.05"
    assert ledger_module.to_cents(44.17) == 4417 and ledger_module.to_cents("0.1") == 10


def test_the_ledger_never_reads_wall_time() -> None:
    assert_no_wall_clock(ledger_module)
