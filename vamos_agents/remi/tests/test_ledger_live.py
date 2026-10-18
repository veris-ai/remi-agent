"""``FineractLedger`` against a real Fineract seeded as the World (opt-in).

Set ``REMI_TEST_FINERACT_URL`` to a Fineract that the benchmark repo's World seeder has
seeded (``FINERACT_API_BASE=… python -m collections_bench.world.fineract --seed``),
with its business date on 2027-02-10, as the twin holds it; a local one runs from
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
TODAY = date(2027, 2, 10)


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


async def _scratch_loan(ledger: FineractLedger, principal: str = "265.00") -> str:
    """A placed account of the test's own: client, loan, itemization, disbursed on 2025-03-29."""
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
            "activationDate": "29 March 2025",
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
            "expectedDisbursementDate": "29 March 2025",
            "submittedOnDate": "29 March 2025",
            "datatables": [
                {
                    "registeredTableName": "tas_itemization",
                    "data": {
                        "charge_off_on": "29 March 2025",
                        "charge_off_balance": principal,
                        "interest_since_charge_off": 0,
                        "fees_since_charge_off": 0,
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
        json={"approvedOnDate": "29 March 2025", **FORMAT},
    )
    await call(
        "POST",
        f"/loans/{loan_id}",
        params={"command": "disburse"},
        json={"actualDisbursementDate": "29 March 2025", "transactionAmount": principal, **FORMAT},
    )
    return external_id


async def test_a_seeded_account_reads_as_the_world_holds_it(ledger) -> None:
    assert ledger.business_date == TODAY and set(ledger.payment_types) == {"card", "ach"}
    debt = await ledger.debt("TAS-420103")  # interest, fees and a payment before placement
    item = debt.itemization
    assert (
        item.charge_off_balance_cents,
        item.interest_since_charge_off_cents,
        item.fees_since_charge_off_cents,
    ) == (27500, 2975, 1800)
    assert (debt.paid_cents, debt.balance_cents, debt.last_payment_on, debt.open) == (
        1000,
        31275,
        date(2025, 6, 27),
        True,
    )


async def test_post_settle_and_re_age_on_the_real_engine(ledger) -> None:
    settled = await ledger.debt(await _scratch_loan(ledger))
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    posting = await ledger.post_payment(
        settled, amount_cents=18550, on=TODAY, kind="card", payment_id=payment_id, processor_ref="rp_1"
    )
    again = await ledger.post_payment(
        settled, amount_cents=18550, on=TODAY, kind="card", payment_id=payment_id, processor_ref="rp_1"
    )
    assert again.transaction_id == posting.transaction_id  # posted once
    await ledger.write_off(settled, on=TODAY, external_id=f"wo_{payment_id}", note="Settled.")
    after = await ledger.debt(settled.account_id)
    assert (after.status, after.balance_cents, after.paid_cents) == (601, 0, 18550)

    plan = await ledger.debt(await _scratch_loan(ledger))
    aged = await ledger.re_age(
        plan, instalments=6, first_due_on=date(2027, 3, 1), external_id=f"arr_{uuid.uuid4().hex[:8]}"
    )
    future = [(i.due_on, i.amount_cents) for i in aged.schedule if i.due_on > TODAY]
    assert future == [(date(2027, m, 1), 4417) for m in range(3, 8)] + [(date(2027, 8, 1), 4415)]
    with pytest.raises(LedgerRefused, match="once a day") as twice:
        await ledger.re_age(aged, instalments=3, first_due_on=date(2027, 3, 5), external_id="arr_again")
    assert twice.value.code == "error.msg.loan.reage.reage.transaction.already.present.for.today"
    await ledger.undo_re_age(aged)
    with pytest.raises(LedgerRefused) as future_dated:
        await ledger.post_payment(
            plan,
            amount_cents=100,
            on=date(2027, 2, 11),
            kind="ach",
            payment_id="pay_future",
            processor_ref="rp",
        )
    assert future_dated.value.code == "error.msg.loan.transaction.cannot.be.a.future.date"
