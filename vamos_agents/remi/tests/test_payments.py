"""Remi's PayNearMe client on the wire: signed as the twin checks it, with the World's time, answers reported as given.

World v2 takes payments on PayNearMe (WORLD.md, "Payments: PayNearMe"): every request is
``POST /json-api/<operation>`` with ``version`` 3.0, the site id, a ``timestamp`` within 300 seconds of the
twin's virtual clock and an HMAC-SHA256 ``signature`` over the sorted parameters. A decline is PayNearMe's
answer, not an error; a refusal (its error envelope) is PayNearMe's words; an unreachable twin is
``PaymentsUnavailable``; an answer that cannot be read is ``UnreadableAnswer``. Card and bank numbers go to
PayNearMe and nowhere else.
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
    UnreadableAnswer,
    sign,
)

from .fake_paynearme import (
    HMAC_SECRET,
    SITE_IDENTIFIER,
    FakePayNearMe,
    signature,
)
from .support import BANK_BILLING, CARD_BILLING, MANIFEST

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
        **CARD_BILLING,
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
        order=ORDER, routing="110000000", account="000123456789", **BANK_BILLING, at=AT
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
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", **CARD_BILLING, at=AT
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
        order=ORDER, number="4000000000009995", exp_month=11, exp_year=2029, cvc="123", **CARD_BILLING, at=AT
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
            **CARD_BILLING,
            at=AT,
        )
    _, wrong_secret = twin_and_client(secret="not-the-secret")
    with pytest.raises(PayNearMeRefused, match="Authentication failed"):
        await wrong_secret.add_card(
            order=ORDER,
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            **CARD_BILLING,
            at=AT,
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
            **CARD_BILLING,
            at=AT + timedelta(seconds=301),
        )
    await client.close()
    await wrong_secret.close()


async def test_an_unreachable_or_failing_twin_is_unavailable() -> None:
    twin, client = twin_and_client()
    twin.unreachable = True
    with pytest.raises(PaymentsUnavailable, match="could not be reached"):
        await client.add_card(
            order=ORDER,
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            **CARD_BILLING,
            at=AT,
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
            order=ORDER,
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            **CARD_BILLING,
            at=AT,
        )
    await failing.close()


def _payment_changed(answer: dict, **changed) -> httpx.Response:
    payment = {k: v for k, v in {**answer["payment"], **changed}.items() if v is not None}
    return httpx.Response(200, json={**answer, "payment": payment})


@pytest.mark.parametrize(
    ("reshape", "why"),
    [
        (lambda answer: httpx.Response(200, text="<html>504</html>"), "not JSON"),
        (lambda answer: httpx.Response(200, json=["ok"]), "not a JSON object"),
        (lambda answer: httpx.Response(200, json={"payment": answer["payment"]}), "status"),
        (lambda answer: httpx.Response(200, json={"status": "ok"}), "payment"),
        (lambda answer: _payment_changed(answer, pnm_payment_identifier=None), "pnm_payment_identifier"),
        (lambda answer: _payment_changed(answer, payment_status=""), "payment_status"),
        (lambda answer: _payment_changed(answer, net_payment_amount=None), "net_payment_amount"),
        (lambda answer: _payment_changed(answer, net_payment_amount="12 dollars"), "net_payment_amount"),
        (lambda answer: _payment_changed(answer, net_payment_amount="Infinity"), "net_payment_amount"),
    ],
    ids=[
        "not JSON",
        "not a JSON object",
        "no envelope status",
        "no payment",
        "no payment id",
        "blank payment status",
        "no net amount",
        "an amount that is not one",
        "an infinite amount",
    ],
)
async def test_a_charge_answered_unreadably_is_unreadable_not_unavailable(reshape, why, caplog) -> None:
    """PayNearMe answered 2xx and took the charge, but the answer cannot be read: ``UnreadableAnswer`` naming what
    could not be read, logged loudly without the answer itself or any card data."""
    caplog.set_level(logging.DEBUG)
    twin, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="987", **CARD_BILLING, at=AT
    )
    twin.reshape = lambda operation, answer: reshape(answer)
    with pytest.raises(
        UnreadableAnswer, match=f"PayNearMe's answer to make_payment could not be read: .*{why}"
    ):
        await client.pay(order=ORDER, method=card, amount_cents=10_000, at=AT)
    assert [p["status"] for p in twin.payments] == ["approved"]
    assert any(r.levelno >= logging.ERROR and why in r.getMessage() for r in caplog.records)
    assert "4111111111111111" not in caplog.text and "987" not in caplog.text
    await client.close()


@pytest.mark.parametrize(
    "reshape",
    [
        lambda answer: httpx.Response(200, content=b"\xff\xfe not json"),
        lambda answer: httpx.Response(200, json={"status": "ok", "order": {"electronic_payments": {}}}),
    ],
    ids=["not JSON", "no method"],
)
async def test_a_saved_method_answered_unreadably_is_unreadable(reshape) -> None:
    twin, client = twin_and_client()
    twin.reshape = lambda operation, answer: reshape(answer)
    with pytest.raises(
        UnreadableAnswer, match="PayNearMe's answer to create_payment_method could not be read"
    ):
        await client.add_card(
            order=ORDER,
            number="4111111111111111",
            exp_month=8,
            exp_year=2028,
            cvc="123",
            **CARD_BILLING,
            at=AT,
        )
    await client.close()


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
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", **CARD_BILLING, at=AT
    )
    assert seen == ["https://twin.example/json-api/create_payment_method"]
    await client.close()


async def test_card_details_are_never_logged_or_kept(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="987", **CARD_BILLING, at=AT
    )
    await client.pay(order=ORDER, method=card, amount_cents=100, at=AT)
    for text in (caplog.text, repr(card), repr(client.__dict__)):
        assert "4111111111111111" not in text and "987" not in text and HMAC_SECRET not in text
    await client.close()


#: The twin's own words (services-sandbox services/paynearme), asserted as literals.
BAD_CHECKSUM = "Card number isn’t valid (bad checksum)"
BAD_ABA = "ABA isn’t valid (bad checksum)"
BAD_ACCOUNT = "This account number is invalid"
BAD_SECURITY_CODE = "This payment didn't complete because the security code was invalid."


@pytest.mark.parametrize(
    ("details", "words"),
    [
        (
            {"number": "4111111111111112", "exp_month": 8, "exp_year": 2028, "cvc": "123"},
            BAD_CHECKSUM,
        ),  # Luhn
        ({"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "12"}, BAD_SECURITY_CODE),
        ({"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "1234"}, BAD_SECURITY_CODE),
        (
            {"number": "378282246310005", "exp_month": 8, "exp_year": 2028, "cvc": "123"},
            BAD_SECURITY_CODE,
        ),  # Amex: 4
    ],
)
async def test_the_twins_card_validation_is_reported_in_its_words(details: dict, words: str) -> None:
    twin, client = twin_and_client()
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_card(order=ORDER, **CARD_BILLING, at=AT, **details)
    assert str(refused.value) == f"PayNearMe refused it: {words}"
    assert twin.methods == {}
    amex = await client.add_card(
        order=ORDER, number="378282246310005", exp_month=8, exp_year=2028, cvc="1234", **CARD_BILLING, at=AT
    )
    assert (amex.brand, amex.last4) == ("American Express", "0005")
    await client.close()


@pytest.mark.parametrize(
    ("routing", "account", "words"),
    [("110000001", "000123456789", BAD_ABA), ("110000000", "123", BAD_ACCOUNT)],
)
async def test_the_twins_bank_validation_is_reported_in_its_words(
    routing: str, account: str, words: str
) -> None:
    _, client = twin_and_client()
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_bank_account(order=ORDER, routing=routing, account=account, **BANK_BILLING, at=AT)
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
        order=ORDER, number=number, exp_month=exp_month, exp_year=exp_year, cvc="123", **CARD_BILLING, at=AT
    )
    paid = await client.pay(order=ORDER, method=card, amount_cents=10_000, at=AT)
    assert (paid.status, paid.response_code, paid.response_message) == ("rejected", code, words)
    await client.close()


@pytest.mark.parametrize(
    ("number", "exp_month", "exp_year", "code", "words"),
    [
        (
            "4111111111111111",
            10,
            2026,
            "54",
            "This card is expired.",
        ),  # accepted when saved, declined at charge
        ("411111111111116", 8, 2028, "14", "This card number is invalid."),  # a length Visa does not issue
    ],
)
async def test_a_card_the_twin_saves_can_still_decline_at_charge(
    number: str, exp_month: int, exp_year: int, code: str, words: str
) -> None:
    twin, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number=number, exp_month=exp_month, exp_year=exp_year, cvc="123", **CARD_BILLING, at=AT
    )
    paid = await client.pay(order=ORDER, method=card, amount_cents=10_000, at=AT)
    assert (paid.status, paid.response_code, paid.response_message) == ("rejected", code, words)
    assert twin.payments[-1]["status"] == "rejected"
    await client.close()


async def test_a_bad_security_code_sent_at_charge_is_a_rejected_payment_nz() -> None:
    """The double as the twin: a card charge's cvv_pii, when sent, declines NZ when it is the wrong length for the
    card (Remi sends the security code only when the card is saved)."""
    twin, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=8, exp_year=2028, cvc="123", **CARD_BILLING, at=AT
    )
    body = {
        "version": "3.0",
        "site_identifier": SITE_IDENTIFIER,
        "timestamp": str(int(AT.timestamp())),
        "pnm_order_identifier": ORDER,
        "payment_method_identifier": card.method_id,
        "payment_amount": "100.00",
        "payment_currency": "USD",
        "cvv_pii": "12",
    }
    body["signature"] = signature(body, HMAC_SECRET)
    answer = twin.handle(
        httpx.Request("POST", "https://twin.example/json-api/make_payment", json=body)
    ).json()
    assert (answer["payment"]["payment_status"], answer["payment"]["response_code"]) == ("rejected", "NZ")
    assert (
        answer["payment"]["response_message"]
        == "This payment didn't complete because the security code was invalid."
    )
    await client.close()


@pytest.mark.parametrize(
    ("missing", "field"),
    [
        ("cvc", "payment_method_cvv_pii"),
        ("billing_name", "payment_method_billing_name"),
        ("billing_address", "payment_method_billing_address"),
        ("billing_zip", "payment_method_billing_zipcode"),
    ],
)
async def test_a_card_needs_what_paynearme_requires(missing: str, field: str) -> None:
    _, client = twin_and_client()
    details = {"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "123", **CARD_BILLING}
    del details[missing]
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_card(order=ORDER, at=AT, **details)
    assert str(refused.value) == f"PayNearMe refused it: Missing required parameter: {field}"
    await client.close()


@pytest.mark.parametrize(
    ("missing", "field"),
    [("billing_name", "payment_method_billing_name"), ("billing_zip", "payment_method_billing_zipcode")],
)
async def test_a_bank_account_needs_what_paynearme_requires(missing: str, field: str) -> None:
    _, client = twin_and_client()
    details = {"routing": "110000000", "account": "000123456789", **BANK_BILLING}
    del details[missing]
    with pytest.raises(PayNearMeRefused) as refused:
        await client.add_bank_account(order=ORDER, at=AT, **details)
    assert str(refused.value) == f"PayNearMe refused it: Missing required parameter: {field}"
    await client.close()


async def test_the_billing_zip_is_5_or_9_digits_and_the_phone_is_optional() -> None:
    twin, client = twin_and_client()
    card = {"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "123", **CARD_BILLING}
    with pytest.raises(PayNearMeRefused, match=r"^PayNearMe refused it: Billing ZIP code is invalid$"):
        await client.add_card(order=ORDER, at=AT, **{**card, "billing_zip": "1121"})
    await client.add_card(order=ORDER, at=AT, **card)
    assert "payment_method_billing_phone" not in twin.requests[-1][1]
    await client.add_card(
        order=ORDER, at=AT, **{**card, "billing_zip": "11211-1234", "billing_phone": "3475550118"}
    )
    _, body = twin.requests[-1]
    assert (body["payment_method_billing_zipcode"], body["payment_method_billing_phone"]) == (
        "11211-1234",
        "3475550118",
    )
    assert (body["payment_method_billing_name"], body["payment_method_billing_address"]) == (
        CARD_BILLING["billing_name"],
        CARD_BILLING["billing_address"],
    )
    await client.close()


@pytest.mark.parametrize(
    ("zipcode", "accepted"),
    [
        ("75013", True),
        ("75013-1234", True),
        ("750131234", True),
        ("75013 1234", True),
        ("75013  1234", False),
        ("75013\t1234", False),
        ("75013 -1234", False),
        ("7501", False),
    ],
)
async def test_a_zip_plus_4_takes_one_space_or_one_hyphen(zipcode: str, accepted: bool) -> None:
    twin, client = twin_and_client()
    card = {"number": "4111111111111111", "exp_month": 8, "exp_year": 2028, "cvc": "123", **CARD_BILLING}
    if accepted:
        await client.add_card(order=ORDER, at=AT, **{**card, "billing_zip": zipcode})
        assert len(twin.methods) == 1
    else:
        with pytest.raises(PayNearMeRefused, match="Billing ZIP code is invalid"):
            await client.add_card(order=ORDER, at=AT, **{**card, "billing_zip": zipcode})
    await client.close()


def _signed(fields: dict[str, str]) -> dict[str, str]:
    body = {
        "version": "3.0",
        "site_identifier": SITE_IDENTIFIER,
        "timestamp": str(int(AT.timestamp())),
        **fields,
    }
    body["signature"] = signature(body, HMAC_SECRET)
    return body


def _post(twin, operation: str, fields: dict[str, str]) -> dict:
    url = f"https://twin.example/json-api/{operation}"
    return twin.handle(httpx.Request("POST", url, json=_signed(fields))).json()


async def test_skip_ach_verify_skips_the_account_checks_not_the_routing_check() -> None:
    """The double as the twin: verification runs when a bank account is added unless skip_ach_verify is true; the
    routing check always runs; an account added unverified is declined at charge with its verification result.
    (Remi never sends skip_ach_verify.)"""
    twin, client = twin_and_client()
    gs02 = {
        "pnm_order_identifier": ORDER,
        "payment_method_type": "ach",
        "payment_method_ach_aba_pii": "110000000",
        "payment_method_ach_account_pii": "000222222227",
        "payment_method_billing_name": "Marcus Bell",
        "payment_method_billing_zipcode": "11211",
    }
    assert _post(twin, "create_payment_method", gs02) == {
        "status": "error",
        "errors": [{"description": "This account number is invalid"}],
    }
    added = _post(twin, "create_payment_method", {**gs02, "skip_ach_verify": "true"})
    method = added["order"]["electronic_payments"]["payment_methods"][0]["accounts"][0][
        "payment_method_identifier"
    ]
    charge = {
        "pnm_order_identifier": ORDER,
        "payment_method_identifier": method,
        "payment_amount": "100.00",
        "payment_currency": "USD",
    }
    paid = _post(twin, "make_payment", charge)["payment"]
    assert (paid["payment_status"], paid["response_code"]) == ("rejected", "GS02")
    short = {**gs02, "payment_method_ach_account_pii": "123", "skip_ach_verify": "true"}
    assert _post(twin, "create_payment_method", short)["status"] == "ok", "the account length is verification"
    bad_routing = {**gs02, "payment_method_ach_aba_pii": "110000001", "skip_ach_verify": "true"}
    assert _post(twin, "create_payment_method", bad_routing)["errors"] == [
        {"description": "ABA isn’t valid (bad checksum)"}
    ]
    await client.close()


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("-5.00", "Payment amount is invalid"),
        ("0.00", "Payment amount is invalid"),
        ("0.001", "Payment amount is invalid"),
        ("5.001", "Payment amount is invalid"),
        ("abc", "Payment amount is invalid"),
        ("NaN", "Payment amount is invalid"),
        ("Infinity", "Payment amount is invalid"),
        ("1000000000.00", "Payment amount is invalid"),
        ("999999999.99", "Payment amount exceeds the amount remaining on the order"),
        (None, "Missing required parameter: payment_amount"),
        ("", "Missing required parameter: payment_amount"),
    ],
)
async def test_a_payment_amount_is_whole_cents_above_zero_up_to_the_twins_limit(amount, words) -> None:
    """The double as the twin's make_payment (logic.parse_money and required_amount): whole cents, above zero, at
    most $999,999,999.99, else "Payment amount is invalid"; then what the order still accepts."""
    twin, client = twin_and_client()
    card = await client.add_card(
        order=ORDER, number="4111111111111111", exp_month=12, exp_year=2030, cvc="123", **CARD_BILLING, at=AT
    )
    charge = {
        "pnm_order_identifier": ORDER,
        "payment_method_identifier": card.method_id,
        "payment_currency": "USD",
    }
    if amount is not None:
        charge["payment_amount"] = amount
    assert _post(twin, "make_payment", charge) == {"status": "error", "errors": [{"description": words}]}
    assert twin.payments == []
    await client.close()
