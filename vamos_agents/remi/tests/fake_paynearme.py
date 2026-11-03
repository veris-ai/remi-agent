"""The PayNearMe twin's vendor API as a test double: what Remi's payments client relies on, served in memory.

The twin is services-sandbox ``services/paynearme`` (PayNearMe JSON API v3.0)
with services-sandbox #2054, seeded by World v2 (``world_v2/paynearme.py``):
one ``up-to`` order per placed account, no fee, and World v1's test instruments.
This double keeps its rules, as read from that code:

- every request is ``POST /json-api/<operation>`` with a JSON body carrying
  ``version`` ``3.0``, ``site_identifier``, ``timestamp`` and ``signature``:
  HMAC-SHA256 with the site's secret over each parameter's name and value,
  sorted by name, the twin's excluded names left out. A wrong signature or site
  id, or a ``timestamp`` more than 300 seconds from the twin's **virtual**
  clock, answers ``Authentication failed``;
- ``create_payment_method`` tokenizes a card (number, MM/YYYY or MM/YY expiry)
  or bank account (routing, account) on an order and answers only the new
  method; ``make_payment`` charges it: a test instrument's outcome by its
  fingerprint, a card past its expiry month ``54``, anything else approved; an
  up-to order refuses a payment that would take its payments above its amount;
- a refusal is ``{"status": "error", "errors": [{"description": ...}]}``.

Every request is kept in ``requests`` (operation and body), as the twin's log
would show it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

SITE_IDENTIFIER = "S9921000001"
HMAC_SECRET = "twin-hmac-secret-for-tests"
#: The parameters the twin leaves out of the signature (services/paynearme ``_canonical``).
EXCLUDED = frozenset(
    {"signature", "call", "action", "controller", "format", "fp", "print_buttons", "datafile", "api",
     "business_document", "bank_document", "return_url", "afo", "app", "site_id", "iframe", "locale", "x", "y"}
)  # fmt: skip
WINDOW_S = 300


def instrument_fingerprint(kind: str, *parts: Any) -> str:
    """The twin's keyless fingerprint (``logic.instrument_fingerprint``)."""
    normalized = "|".join(re.sub(r"[^0-9A-Za-z/]", "", str(part)) for part in parts)
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


def card_expiry(raw: Any) -> str | None:
    """MM/YYYY from MM/YYYY or MM/YY (the twin's ``card_expiry``)."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*", str(raw or ""))
    if not match or not 1 <= int(match.group(1)) <= 12:
        return None
    year = match.group(2) if len(match.group(2)) == 4 else f"20{match.group(2)}"
    return f"{int(match.group(1)):02d}/{year}"


def signature(body: dict[str, Any], secret: str) -> str:
    canonical = "".join(f"{key}{body[key]}" for key in sorted(body) if key not in EXCLUDED)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _error(description: str) -> dict[str, Any]:
    return {"status": "error", "errors": [{"description": description}]}


def _money(minor: int) -> str:
    return f"{Decimal(minor) / 100:.2f}"


class FakePayNearMe:
    """World v2's PayNearMe for one test: the manifest's orders and instruments, a frozen virtual clock."""

    def __init__(self, orders: list[dict[str, Any]], behaviors: list[dict[str, Any]], now: int) -> None:
        self.site_identifier, self.hmac_secret = SITE_IDENTIFIER, HMAC_SECRET
        self.now = now  # the twin's virtual clock, epoch seconds
        self.orders = {o["pnm_order_identifier"]: o for o in orders}
        self.behaviors = {b["fingerprint"]: b for b in behaviors}
        self.methods: dict[str, dict[str, Any]] = {}
        self.payments: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.unreachable = False  # every request fails to connect while set
        self._ids = 0

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> FakePayNearMe:
        noon = datetime.fromisoformat(f"{manifest['call_date']}T12:00:00-05:00")
        return cls(
            manifest["paynearme"]["orders"],
            manifest["paynearme"]["instrument_behaviors"],
            int(noon.timestamp()),
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.requests.append((operation, body))
        if not request.url.path.endswith(f"/json-api/{operation}") or request.method != "POST":
            return httpx.Response(404, json={"detail": "Not Found"})
        if not self._authentic(body):
            return httpx.Response(200, json=_error("Authentication failed"))
        handler = {
            "create_payment_method": self._create_payment_method,
            "make_payment": self._make_payment,
        }.get(operation)
        if handler is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=handler(body))

    def _authentic(self, body: dict[str, Any]) -> bool:
        try:
            timestamp_ok = abs(int(body.get("timestamp", 0)) - self.now) <= WINDOW_S
        except (TypeError, ValueError):
            timestamp_ok = False
        signed = hmac.compare_digest(str(body.get("signature", "")), signature(body, self.hmac_secret))
        return (
            timestamp_ok
            and signed
            and str(body.get("site_identifier")) == self.site_identifier
            and str(body.get("version")) == "3.0"
        )

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:010d}"

    def _create_payment_method(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("payment_method_type")
        if kind not in ("card", "ach"):
            return _error("Payment method type is invalid")
        order = self.orders.get(str(body.get("pnm_order_identifier")))
        if order is None:
            return _error("Order not found")
        if kind == "card":
            number, raw_expiry = (
                body.get("payment_method_card_number_pii"),
                body.get("payment_method_card_expiry_pii"),
            )
            if not number or not raw_expiry:
                return _error("Card number and expiration are required")
            expiry = card_expiry(raw_expiry)
            if expiry is None:
                return _error("Card expiration date is invalid")
            fingerprint = instrument_fingerprint("card", number, expiry)
            account = {
                "description": "Card",
                "account_type": "Credit",
                "expiration_date": expiry,
                "card_brand": "Visa",
            }
        else:
            number, aba = body.get("payment_method_ach_account_pii"), body.get("payment_method_ach_aba_pii")
            if not number or not aba:
                return _error("Routing and account numbers are required")
            fingerprint, expiry = instrument_fingerprint("ach", aba, number), None
            account = {"description": "Bank Account", "account_type": "Personal Checking"}
        method_id = self._next_id("31")
        last4 = re.sub(r"\D", "", str(number))[-4:]
        account = {
            "payment_method_identifier": method_id,
            "status": "active",
            "name": body.get("payment_method_billing_name", "Account Holder"),
            "number": last4,
            **account,
        }
        self.methods[method_id] = {
            "order": order["pnm_order_identifier"],
            "type": kind,
            "fingerprint": fingerprint,
            "expiry": expiry,
            "account": account,
        }
        return {
            "status": "ok",
            "order": {
                "pnm_order_identifier": order["pnm_order_identifier"],
                "electronic_payments": {"payment_methods": [{"type": kind, "accounts": [account]}]},
            },
        }

    def _make_payment(self, body: dict[str, Any]) -> dict[str, Any]:
        order = self.orders.get(str(body.get("pnm_order_identifier")))
        method = self.methods.get(str(body.get("payment_method_identifier")))
        if order is None or method is None or method["order"] != order["pnm_order_identifier"]:
            return _error("Order or payment method not found")
        try:
            minor = int((Decimal(str(body.get("payment_amount"))) * 100).to_integral_exact())
        except (InvalidOperation, ValueError):
            return _error("Payment amount is invalid")
        if minor <= 0:
            return _error("Payment amount is invalid")
        if str(body.get("payment_currency", "USD")) != "USD":
            return _error("Payment currency does not match the order currency")
        paid = sum(
            p["amount_minor"]
            for p in self.payments
            if p["order"] == order["pnm_order_identifier"] and p["status"] == "approved"
        )
        if paid + minor > order["order_amount_minor"]:
            return _error("Payment amount exceeds the amount remaining on the order")
        behavior = self.behaviors.get(method["fingerprint"])
        if behavior is not None and behavior["outcome"] in ("decline", "ach_validation_failure"):
            status, code, message = "rejected", behavior["response_code"], behavior["response_message"]
        elif method["expiry"] is not None and self._expired(method["expiry"]):
            status, code, message = "rejected", "54", "This card is expired."
        else:
            status, code, message = "approved", "0", "Approved"
        payment_id = self._next_id("88")
        self.payments.append(
            {"id": payment_id, "order": order["pnm_order_identifier"], "method": body["payment_method_identifier"],
             "amount_minor": minor, "status": status, "channel": body.get("site_channel")}
        )  # fmt: skip
        return {
            "status": "ok",
            "payment": {
                "payment_amount": _money(minor),
                "payment_currency": "USD",
                "payment_status": status,
                "payment_type": method["type"],
                "payment_account": f"{method['account']['description']} {method['account']['number']}",
                "payment_method_identifier": body["payment_method_identifier"],
                "net_payment_amount": _money(minor),
                "net_payment_currency": "USD",
                "payment_processing_fee": "0.00",
                "pnm_payment_identifier": payment_id,
                "site_channel": body.get("site_channel"),
                "pnm_order_identifier": order["pnm_order_identifier"],
                "site_order_identifier": order["site_order_identifier"],
                "response_code": code,
                "response_message": message,
            },
        }

    def _expired(self, expiry: str) -> bool:
        month, year = (int(part) for part in expiry.split("/"))
        today = datetime.fromtimestamp(self.now, UTC)
        return (year, month) < (today.year, today.month)
