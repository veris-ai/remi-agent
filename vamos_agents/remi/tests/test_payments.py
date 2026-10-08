"""``PaymentGateway`` against the World's real gateway functions (vendored ``collections_world.sql``).

Remi connects as the app login, which owns nothing, so the gateway's write
guards bind exactly as they would for a candidate that does not own the World:
these tests prove every payment method and payment Remi leaves came from the
gateway functions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import psycopg
import pytest

from vamos_agents.remi.db import Database
from vamos_agents.remi.payments import BankDetails, CardDetails, PaymentError, PaymentGateway, SavedMethod

from .support import (
    AMEX_CARD,
    DECLINED_CARD,
    DECLINING_BANK,
    EXPIRED_CARD,
    GOOD_BANK,
    GOOD_CARD,
    INSUFFICIENT_FUNDS_CARD,
    all_text,
    app_dsn,
    rows,
    seed_world,
)

pytestmark = pytest.mark.postgres

AT = datetime.fromisoformat("2027-02-10T09:04:12-05:00")
ACCOUNT = "TAS-420101"
HOLDER = "Aaliyah Alvarez"


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[Database]:
    seed_world(pg_dsn)
    database = await Database.connect(app_dsn(pg_dsn))
    yield database
    await database.close()


async def tokenize_card(db: Database, number: str, exp=(12, 2030), cvc: str = "123") -> SavedMethod:
    async with db.transaction() as tx:
        return await PaymentGateway(tx).tokenize_card(
            account_id=ACCOUNT, holder_name=HOLDER, card=CardDetails(number, *exp, cvc), at=AT
        )


async def tokenize_ach(db: Database, bank: tuple[str, str]) -> SavedMethod:
    async with db.transaction() as tx:
        return await PaymentGateway(tx).tokenize_ach(
            account_id=ACCOUNT, holder_name=HOLDER, bank=BankDetails(*bank), at=AT
        )


async def charge(db: Database, method_id: str, amount_cents: int = 5000):
    async with db.transaction() as tx:
        return await PaymentGateway(tx).charge(method_id=method_id, amount_cents=amount_cents, at=AT)


async def test_approved_card_is_saved_and_charged_and_the_account_is_left_to_the_ledger(db, pg_dsn) -> None:
    saved = await tokenize_card(db, GOOD_CARD, exp=(12, 30))
    assert (saved.kind, saved.last4, saved.brand) == ("card", "4242", "visa")
    assert saved.method_id.startswith("pm_")
    assert (await tokenize_card(db, GOOD_CARD)).method_id == saved.method_id  # same card, same method

    result = await charge(db, saved.method_id, 5000)
    assert (result.status, result.decline_code, result.approved) == ("approved", None, True)
    assert result.payment_id.startswith("pay_") and result.processor_ref.startswith("rp_")

    (method,) = rows(pg_dsn, "payment_methods")
    assert (method["exp_month"], method["exp_year"], method["holder_name"]) == (12, 2030, HOLDER)
    assert method["created_at"] == AT
    (payment,) = rows(pg_dsn, "payments")
    assert (payment["payment_id"], payment["method_id"], payment["amount_cents"], payment["at"]) == (
        result.payment_id,
        saved.method_id,
        5000,
        AT,
    )
    # A processor does not know the agency's balance: posting the charge is the ledger's (ledger.py).
    assert rows(pg_dsn, "accounts")[0]["status"] == "active"


@pytest.mark.parametrize(
    ("number", "decline_code"),
    [
        (INSUFFICIENT_FUNDS_CARD, "insufficient_funds"),
        (DECLINED_CARD, "card_declined"),
        (EXPIRED_CARD, "expired_card"),
    ],
)
async def test_each_declining_card(db, pg_dsn, number: str, decline_code: str) -> None:
    saved = await tokenize_card(db, number)
    result = await charge(db, saved.method_id, 18500)
    assert (result.status, result.decline_code, result.approved) == ("declined", decline_code, False)
    (payment,) = rows(pg_dsn, "payments")
    assert (payment["status"], payment["decline_code"]) == ("declined", decline_code)
    assert rows(pg_dsn, "accounts")[0]["status"] == "active"


async def test_ach_approves_and_declines(db, pg_dsn) -> None:
    good = await tokenize_ach(db, GOOD_BANK)
    bad = await tokenize_ach(db, DECLINING_BANK)
    assert (good.kind, good.last4, good.brand) == ("ach", "6789", None)
    assert bad.last4 == "2227"
    assert (await charge(db, bad.method_id, 1000)).decline_code == "insufficient_funds"
    assert (await charge(db, good.method_id, 1000)).approved
    assert [
        (p["status"], p["decline_code"]) for p in rows(pg_dsn, "payments", "true order by at, status")
    ] == [
        ("approved", None),
        ("declined", "insufficient_funds"),
    ]


@pytest.mark.parametrize(
    ("tokenize", "message"),
    [
        (lambda db: tokenize_card(db, "4242424242424241"), "fails the Luhn check"),
        (lambda db: tokenize_card(db, GOOD_CARD, exp=(1, 2027)), "the card expired at the end of 01/2027"),
        (lambda db: tokenize_card(db, GOOD_CARD, cvc="12"), "the security code must be 3 digits"),
        (lambda db: tokenize_card(db, AMEX_CARD, cvc="123"), "the security code must be 4 digits"),
        (lambda db: tokenize_ach(db, ("110000001", "000123456789")), "fails the ABA checksum"),
        (lambda db: tokenize_ach(db, ("110000000", "12")), "4 to 17 digits"),
    ],
)
async def test_invalid_instruments_are_refused_in_the_gateways_words(db, pg_dsn, tokenize, message) -> None:
    with pytest.raises(PaymentError) as refused:
        await tokenize(db)
    assert str(refused.value).startswith("The payment gateway refused it: ")
    assert message in str(refused.value) and "gateway:" not in str(refused.value)
    for secret in (GOOD_CARD, "4242424242424241", AMEX_CARD, "000123456789"):
        assert secret not in str(refused.value)
    assert rows(pg_dsn, "payment_methods") == []


async def test_charge_errors_write_nothing(db, pg_dsn) -> None:
    saved = await tokenize_card(db, GOOD_CARD)
    with pytest.raises(PaymentError, match="the amount must be more than zero"):
        await charge(db, saved.method_id, 0)
    with pytest.raises(PaymentError, match="no payment method pm_nope"):
        await charge(db, "pm_nope")
    assert rows(pg_dsn, "payments") == []


async def test_card_data_is_never_stored_and_never_in_a_repr(db, pg_dsn) -> None:
    await tokenize_card(db, AMEX_CARD, exp=(3, 2031), cvc="8642")
    await tokenize_ach(db, GOOD_BANK)
    stored = all_text(pg_dsn)  # every table, the processor's vault included (hashes only)
    for secret in (AMEX_CARD, "8642", GOOD_BANK[1]):
        assert secret not in stored
    details = repr(CardDetails(AMEX_CARD, 3, 2031, "8642")) + repr(BankDetails(*GOOD_BANK))
    assert AMEX_CARD not in details and "8642" not in details and GOOD_BANK[1] not in details


async def test_the_app_login_cannot_bypass_the_gateway(db, pg_dsn) -> None:
    """The test setup binds the guards, so every row the other tests see came from the gateway."""
    saved = await tokenize_card(db, GOOD_CARD)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="written only by the gateway"):
        async with db.transaction() as tx:
            await tx._conn.execute(
                "insert into payments (payment_id, account_id, method_id, amount_cents, status, processor_ref, at) "
                "values ('pay_x', %s, %s, 100, 'approved', 'rp_x', %s)",
                (ACCOUNT, saved.method_id, AT),
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction() as tx:
            await tx._conn.execute("select * from processor_vault")
    with pytest.raises(ValueError, match="not a payment-gateway function"):
        async with db.transaction() as tx:
            await tx.call_gateway("processor_outcome_for", {"outcome_key": "x"})
