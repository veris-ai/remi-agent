"""Remi's PayNearMe client on the wire: signed as the twin checks it, with the World's time, answers reported as given.

World v2 takes payments on PayNearMe (WORLD.md, "Payments: PayNearMe"): every request is
``POST /json-api/<operation>`` with ``version`` 3.0, the site id, a ``timestamp`` within 300 seconds of the
twin's virtual clock and an HMAC-SHA256 ``signature`` over the sorted parameters. A decline is PayNearMe's
answer, not an error; a refusal (its error envelope) is PayNearMe's words; an unreachable twin is
``PaymentsUnavailable``. Card and bank numbers go to PayNearMe and nowhere else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
import pytest

from vamos_agents.remi.payments import (
    Payment,
    PaymentsUnavailable,
    PayNearMe,
    PayNearMeRefused,
    SavedMethod,
    sign,
)

from .fake_paynearme import (
    EXPIRED_CARD,
    HMAC_SECRET,
    INVALID_ACCOUNT_NUMBER,
    INVALID_CARD_NUMBER,
    INVALID_ROUTING_NUMBER,
    INVALID_SECURITY_CODE,
    SITE_IDENTIFIER,
    FakePayNearMe,
    signature,
)
from .support import MANIFEST

AT = datetime.fromisoformat("2026-11-03T11:20:00-05:00")
ORDER = MANIFEST["paynearme"]["orders"][0]["pnm_order_identifier"]  # TAS-261101's, $2,400.00 up to


def twin_and_client(
    base: str = "https://twin.example", secret: str = HMAC_SECRET
) -> tuple[FakePayNearMe, PayNearMe]:
    twin = FakePayNearMe.from_manifest(MANIFEST)
    twin.now = int(AT.timestamp())
    return twin, PayNearMe(base, SITE_IDENTIFIER, secret, transport=twin.transport())


async def test_a_request_is_version_3_signed_and_timestamped_with_the_time_given() -> None:
    twin, client = twin_and_client()
    later = AT + timedelta(seconds=37)
    saved = await client.add_card(
        order=ORDER,
        number="4111111111111111",
        exp_month=8,
        exp_year=2028,
        cvc="123",
        holder="Marcus Bell",
        at=later,
    )
    assert saved == SavedMethod(saved.method_id, "card", "1111", "Visa")
    operation, body = twin.requests[0]
    assert operation == "create_payment_method"
    assert (body["version"], body["site_identifier"], body["timestamp"]) == (
        "3.0",
        SITE_IDENTIFIER,
        str(int(later.timestamp())),
    )
    assert body["signature"] == signature(body, HMAC_SECRET) == sign(body, HMAC_SECRET)
    assert (body["pnm_order_identifier"], body["payment_method_type"]) == (ORDER, "card")
    assert (body["payment_method_card_expiry_pii"], body["payment_method_billing_name"]) == (
        "08/2028",
        "Marcus Bell",
    )
    assert all(isinstance(v, str) for v in body.values()), "every parameter signs as the string it is sent as"
    await client.close()


async def test_a_bank_account_is_tokenized_with_its_routing_number() -> None:
    twin, client = twin_and_client()
    saved = await client.add_bank_account(
        order=ORDER, routing="110000000", account="000123456789", holder="Marcus Bell", at=AT
    )
    assert saved == SavedMethod(saved.method_id, "ach", "6789", None)
    _, body = twin.requests[0]
    assert (body["payment_method_type"], body["payment_method_ach_aba_pii"], body["payment_method_ach_account_pii"]) == (
        "ach", "110000000", "000123456789",
    )  # fmt: skip
    await client.close()


async def test_a_payment_is_paynearmes_answer() -> None:
    twin, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", holder="M B", at=AT
    )
    paid = await client.pay(order=ORDER, method=card, amount_cents=144_000, at=AT)
    assert paid == Payment(
        twin.payments[0]["id"], "approved", 144_000, "0", "Approved", card.method_id, "card", "1111"
    )
    _, body = twin.requests[-1]
    assert (body["payment_amount"], body["payment_currency"], body["site_channel"]) == (
        "1440.00",
        "USD",
        "agent",
    )
    await client.close()


async def test_a_decline_is_an_answer_not_an_error() -> None:
    _, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4000000000009995", exp_month=11, exp_year=2029, cvc="123", holder="M B", at=AT
    )
    paid = await client.pay(order=ORDER, method=card, amount_cents=240_000, at=AT)
    assert (paid.status, paid.response_code, paid.amount_cents) == ("rejected", "51", 240_000)
    assert paid.response_message == "There are insufficient funds on this card to complete this payment."
    await client.close()


async def test_a_refusal_is_paynearmes_own_words() -> None:
    _, client = twin_and_client()
    with pytest.raises(PayNearMeRefused, match=r"^PayNearMe refused it: Order not found$"):
        await client.add_card(
            order="000000000000",
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            holder="M B",
            at=AT,
        )
    _, wrong_secret = twin_and_client(secret="not-the-secret")
    with pytest.raises(PayNearMeRefused, match="Authentication failed"):
        await wrong_secret.add_card(
            order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", holder="M B", at=AT
        )
    with pytest.raises(
        PayNearMeRefused, match="Authentication failed"
    ):  # signed with wall time, say: 301 s off
        await client.add_card(
            order=ORDER,
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            holder="M B",
            at=AT + timedelta(seconds=301),
        )
    await client.close()
    await wrong_secret.close()


async def test_an_unreachable_or_failing_twin_is_unavailable() -> None:
    twin, client = twin_and_client()
    twin.unreachable = True
    with pytest.raises(PaymentsUnavailable, match="could not be reached"):
        await client.add_card(
            order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", holder="M B", at=AT
        )
    await client.close()
    failing = PayNearMe(
        "https://twin.example",
        SITE_IDENTIFIER,
        HMAC_SECRET,
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")),
    )
    with pytest.raises(PaymentsUnavailable, match="502"):
        await failing.add_card(
            order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", holder="M B", at=AT
        )
    await failing.close()


@pytest.mark.parametrize(
    "base", ["https://twin.example", "https://twin.example/", "https://twin.example/json-api"]
)
async def test_the_base_may_end_in_json_api_or_not(base: str) -> None:
    seen: list[str] = []
    twin = FakePayNearMe.from_manifest(MANIFEST)
    twin.now = int(AT.timestamp())

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return twin.handle(request)

    client = PayNearMe(base, SITE_IDENTIFIER, HMAC_SECRET, transport=httpx.MockTransport(handle))
    await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", holder="M B", at=AT
    )
    assert seen == ["https://twin.example/json-api/create_payment_method"]
    await client.close()


async def test_card_details_are_never_logged_or_kept(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="987", holder="M B", at=AT
    )
    await client.pay(order=ORDER, method=card, amount_cents=100, at=AT)
    for text in (caplog.text, repr(card), repr(client.__dict__)):
        assert "4111111111111111" not in text and "987" not in text and HMAC_SECRET not in text
    await client.close()


@pytest.mark.parametrize(
    ("details", "words"),
    [
        (
            {"number": "4111111111111112", "exp_month": 8, "exp_year": 2028, "cvc": "123"},
            INVALID_CARD_NUMBER,
        ),  # Luhn
        (
            {"number": "411111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "123"},
            INVALID_CARD_NUMBER,
        ),  # 15-digit Visa
        ({"number": "4111111111111111", "exp_month": 10, "exp_year": 2026, "cvc": "123"}, EXPIRED_CARD),
        (
            {"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "12"},
            INVALID_SECURITY_CODE,
        ),
        (
            {"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "1234"},
            INVALID_SECURITY_CODE,
        ),
        (
            {"number": "378282246310005", "exp_month": 8, "exp_year": 2028, "cvc": "123"},
            INVALID_SECURITY_CODE,
        ),  # Amex: 4
    ],
)
async def test_the_twins_card_validation_is_reported_in_its_words(details: dict, words: str) -> None:
    twin, client = twin_and_client()
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_card(order=ORDER, holder="M B", at=AT, **details)
    assert str(refused.value) == f"PayNearMe refused it: {words}"
    assert twin.methods == {}
    amex = await client.add_card(
        order=ORDER, number="378282246310005", exp_month=8, exp_year=2028, cvc="1234", holder="M B", at=AT
    )
    assert (amex.brand, amex.last4) == ("American Express", "0005")
    await client.close()


@pytest.mark.parametrize(
    ("routing", "account", "words"),
    [("110000001", "000123456789", INVALID_ROUTING_NUMBER), ("110000000", "123", INVALID_ACCOUNT_NUMBER)],
)
async def test_the_twins_bank_validation_is_reported_in_its_words(
    routing: str, account: str, words: str
) -> None:
    _, client = twin_and_client()
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_bank_account(order=ORDER, routing=routing, account=account, holder="M B", at=AT)
    assert str(refused.value) == f"PayNearMe refused it: {words}"
    await client.close()


@pytest.mark.parametrize(
    ("number", "exp_month", "exp_year", "code", "words"),
    [
        ("4000000000000002", 10, 2028, "05", "This card is not permitted to make this payment."),
        (
            "4000000000009995",
            11,
            2029,
            "51",
            "There are insufficient funds on this card to complete this payment.",
        ),
        ("4000000000000069", 11, 2031, "54", "This card is expired."),
    ],
)
async def test_the_world_v2_test_cards_decline_as_the_twin_answers(
    number: str, exp_month: int, exp_year: int, code: str, words: str
) -> None:
    _, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number=number, exp_month=exp_month, exp_year=exp_year, cvc="123", holder="M B", at=AT
    )
    paid = await client.pay(order=ORDER, method=card, amount_cents=10_000, at=AT)
    assert (paid.status, paid.response_code, paid.response_message) == ("rejected", code, words)
    await client.close()
