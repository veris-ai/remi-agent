"""StripeGateway against an in-process fake of the Stripe HTTP API (httpx MockTransport).

Checks what the gateway actually sends: to the twin's base URL, form-encoded
the way the SDK does, ACH with an offline mandate, and that a decline comes
back as a failed charge carrying its PaymentIntent id. No stripe globals move.
"""

from __future__ import annotations

import urllib.parse

import httpx
import stripe

from agents.remi.payments import BankDetails, CardDetails, StripeGateway

from .support import DECLINING_CARD, GOOD_CARD


def fake_stripe(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        path = request.url.path
        if path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_1", "object": "customer"})
        if path == "/v1/payment_methods":
            if form["type"] == "card":
                last4 = form["card[number]"][-4:]
                return httpx.Response(
                    200,
                    json={
                        "id": f"pm_{last4}",
                        "object": "payment_method",
                        "type": "card",
                        "card": {"last4": last4, "brand": "visa"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "pm_bank",
                    "object": "payment_method",
                    "type": "us_bank_account",
                    "us_bank_account": {"last4": "6789"},
                },
            )
        if path.endswith("/attach"):
            return httpx.Response(200, json={"id": path.split("/")[3], "object": "payment_method"})
        if path == "/v1/payment_intents":
            if form["payment_method"] == "pm_9995":
                return httpx.Response(
                    402,
                    json={
                        "error": {
                            "type": "card_error",
                            "code": "card_declined",
                            "decline_code": "insufficient_funds",
                            "message": "Your card has insufficient funds.",
                            "payment_intent": {
                                "id": "pi_declined",
                                "object": "payment_intent",
                                "status": "requires_payment_method",
                            },
                        }
                    },
                )
            return httpx.Response(
                200, json={"id": "pi_ok", "object": "payment_intent", "status": "succeeded"}
            )
        return httpx.Response(
            404, json={"error": {"type": "invalid_request_error", "message": f"no route {path}"}}
        )

    return httpx.MockTransport(handler)


async def test_card_charge_and_decline_go_to_the_twin() -> None:
    requests: list[httpx.Request] = []
    before = stripe.api_base
    gateway = StripeGateway(
        "sk_test_twin", "http://stripe-twin.local:12111/", transport=fake_stripe(requests)
    )
    customer = await gateway.create_customer(account_id="TAS-420101", name="Aaliyah Alvarez")
    good = await gateway.save_card(
        customer_id=customer, name="Aaliyah Alvarez", card=CardDetails(GOOD_CARD, 12, 2030, "123")
    )
    bad = await gateway.save_card(
        customer_id=customer, name="Aaliyah Alvarez", card=CardDetails(DECLINING_CARD, 12, 2030, "123")
    )
    ok = await gateway.charge(customer_id=customer, method=good, amount_cents=18500, account_id="TAS-420101")
    declined = await gateway.charge(
        customer_id=customer, method=bad, amount_cents=18500, account_id="TAS-420101"
    )

    assert (ok.status, ok.payment_intent_id) == ("succeeded", "pi_ok")
    assert (declined.status, declined.payment_intent_id) == ("failed", "pi_declined")
    assert "insufficient funds" in declined.decline_message
    assert {r.url.host for r in requests} == {"stripe-twin.local"}
    assert all(r.headers["authorization"] == "Bearer sk_test_twin" for r in requests)
    assert stripe.api_base == before and stripe.api_key is None
    assert "card" in repr(good) and GOOD_CARD not in repr(CardDetails(GOOD_CARD, 1, 2030, "123"))


async def test_ach_uses_an_offline_mandate() -> None:
    requests: list[httpx.Request] = []
    gateway = StripeGateway("sk_test_twin", "http://stripe-twin.local", transport=fake_stripe(requests))
    bank = await gateway.save_bank_account(
        customer_id="cus_1", name="A B", bank=BankDetails("110000000", "000123456789")
    )
    assert (bank.method, bank.last4) == ("ach", "6789")
    result = await gateway.charge(customer_id="cus_1", method=bank, amount_cents=5000, account_id="TAS-1")
    assert result.status == "succeeded"
    intent = dict(urllib.parse.parse_qsl(requests[-1].content.decode()))
    assert intent["payment_method_types[0]"] == "us_bank_account"
    assert intent["mandate_data[customer_acceptance][type]"] == "offline"
