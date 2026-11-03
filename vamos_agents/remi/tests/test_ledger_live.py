"""``FineractLedger`` against a real Fineract seeded as World v2 (opt-in).

Set ``REMI_TEST_FINERACT_URL`` to a Fineract that the benchmark repo's World v2 seeder has
seeded (``FINERACT_API_BASE=… python -m collections_bench.world_v2.fineract --seed``),
with its business date on 2026-11-03, as the twin holds it; a local one runs from
the pinned image (services-sandbox ``services/fineract/docs/fineract-measurements.md``).
Skipped without it. The reads use the World's accounts as seeded; the writes go to a
scratch loan of the test's own on the World's "Placed Debt" product.
"""

from __future__ import annotations

import os
import ssl
import uuid
from datetime import date

import httpx
import pytest

from vamos_agents.remi.ledger import FORMAT, FineractLedger, LedgerRefused

URL = os.environ.get("REMI_TEST_FINERACT_URL")
pytestmark = pytest.mark.skipif(not URL, reason="REMI_TEST_FINERACT_URL not set")
TODAY = date(2026, 11, 3)


def _transport() -> httpx.AsyncHTTPTransport:
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = (
        False,
        ssl.CERT_NONE,
    )  # a local Fineract's self-signed certificate
    return httpx.AsyncHTTPTransport(verify=context)


@pytest.fixture
async def ledger():
    assert URL is not None
    client = FineractLedger(URL, "mifos", "password", "default", transport=_transport())
    await client.connect()
    yield client
    await client.close()


async def _scratch_loan(ledger: FineractLedger, principal: str = "1440.00") -> str:
    """A placed account of the test's own: client, loan, a charge-off itemization, disbursed on 2025-02-14."""
    external_id = f"TEST-{uuid.uuid4().hex[:10]}"
    call = ledger._call
    product = next(p["id"] for p in await call("GET", "/loanproducts") if p["name"] == "Placed Debt")
    client = await call(
        "POST",
        "/clients",
        json={
            "officeId": 1,
            "legalFormId": 1,
            "firstname": "Pat",
            "lastname": "Probe",
            "externalId": f"con_{external_id}",
            "active": True,
            "activationDate": "14 February 2025",
            **FORMAT,
        },
    )
    loan = await call(
        "POST",
        "/loans",
        json={
            "clientId": client["clientId"],
            "productId": product,
            "externalId": external_id,
            "principal": principal,
            "loanType": "individual",
            "loanTermFrequency": 1,
            "loanTermFrequencyType": 2,
            "numberOfRepayments": 1,
            "repaymentEvery": 1,
            "repaymentFrequencyType": 2,
            "interestRatePerPeriod": 0,
            "amortizationType": 1,
            "interestType": 0,
            "interestCalculationPeriodType": 1,
            "transactionProcessingStrategyCode": "advanced-payment-allocation-strategy",
            "expectedDisbursementDate": "14 February 2025",
            "submittedOnDate": "14 February 2025",
            "datatables": [
                {
                    "registeredTableName": "tas_itemization",
                    "data": {
                        "itemized_from": "charge_off",
                        "itemized_on": "14 February 2025",
                        "itemized_balance": principal,
                        "interest_since": 0,
                        "fees_since": 0,
                        **FORMAT,
                    },
                }
            ],
            **FORMAT,
        },
    )
    loan_id = loan["loanId"]
    await call(
        "POST",
        f"/loans/{loan_id}",
        params={"command": "approve"},
        json={"approvedOnDate": "14 February 2025", **FORMAT},
    )
    await call(
        "POST",
        f"/loans/{loan_id}",
        params={"command": "disburse"},
        json={"actualDisbursementDate": "14 February 2025", "transactionAmount": principal, **FORMAT},
    )
    return external_id


async def test_a_seeded_account_reads_as_the_world_holds_it(ledger) -> None:
    assert ledger.business_date == TODAY and set(ledger.payment_types) == {"card", "ach"}
    debt = await ledger.debt("TAS-261101")  # CARD-04: charged off on 2025-02-14, nothing since
    item = debt.itemization
    assert (item.itemized_from, item.itemized_on, item.itemized_balance_cents) == (
        "charge_off",
        date(2025, 2, 14),
        240_000,
    )
    assert (item.interest_since_cents, item.fees_since_cents) == (0, 0)
    assert (debt.paid_cents, debt.balance_cents, debt.last_payment_on, debt.open) == (0, 240_000, None, True)
    hospital = await ledger.debt("TAS-261107")  # HOSP-01: itemized from its last statement
    assert (hospital.itemization.itemized_from, hospital.balance_cents) == ("last_statement", 41_275)


async def test_post_once_and_write_off_on_the_real_engine(ledger) -> None:
    discount = await ledger.debt(await _scratch_loan(ledger, "2400.00"))
    payment_id = f"{uuid.uuid4().int % 10**12:012d}"  # PayNearMe's payment ids are 12 digits
    posting = await ledger.post_payment(
        discount, amount_cents=144_000, on=TODAY, kind="card", payment_id=payment_id
    )
    again = await ledger.post_payment(
        discount, amount_cents=144_000, on=TODAY, kind="card", payment_id=payment_id
    )
    assert again.transaction_id == posting.transaction_id  # posted once
    await ledger.write_off(discount, on=TODAY, external_id=f"wo_{payment_id}", note="Discount paid.")
    after = await ledger.debt(discount.account_id)
    assert (after.status, after.balance_cents, after.paid_cents) == (601, 0, 144_000)

    plan = await ledger.debt(await _scratch_loan(ledger))
    with pytest.raises(LedgerRefused) as future_dated:
        await ledger.post_payment(
            plan, amount_cents=100, on=date(2026, 11, 4), kind="ach", payment_id="880000000099"
        )
    assert future_dated.value.code == "error.msg.loan.transaction.cannot.be.a.future.date"
