"""The PayNearMe twin's vendor API as a test double: what Remi's payments client relies on, served in memory.

The twin is services-sandbox ``services/paynearme`` (PayNearMe JSON API v3.0)
with #2054 (the up-to order, no implied fee, the vendor's error envelope),
#2056 (input validation) and #2058 (required billing fields), seeded by World v2
(``world_v2/paynearme.py``): one ``up-to`` order per placed account, no fee, and
World v1's test instruments. This double keeps its rules and its words, as read
from that code (the stack's head, aec8fdba):

- every request is ``POST /json-api/<operation>`` with a JSON body carrying
  ``version`` ``3.0``, ``site_identifier``, ``timestamp`` and ``signature``:
  HMAC-SHA256 with the site's secret over each parameter's name and value,
  sorted by name, the twin's excluded names left out. A wrong signature or site
  id, or a ``timestamp`` more than 300 seconds from the twin's **virtual**
  clock, answers ``Authentication failed``;
- ``create_payment_method`` validates what a debtor reads out, then tokenizes
  it on the order and answers only the new method. A card: the number's Luhn
  checksum only ("Card number isn’t valid (bad checksum)"), an expiry of MM/YY,
  MM/YYYY, MMYY or MMYYYY ("Invalid expiration date"), and a security code of 3
  digits, 4 for American Express ("This payment didn't complete because the
  security code was invalid."). A bank account: a 9-digit routing number with
  its ABA check digit ("ABA isn’t valid (bad checksum)") and an account number
  of 4 to 17 digits ("This account number is invalid");
- ``make_payment`` charges it, and these decline as a rejected payment, in this
  order: a card number of a length its brand does not issue (``14``, "This card
  number is invalid."), a card past its expiry month (``54``, "This card is
  expired."; an expired card is accepted when saved), a card charge's
  ``cvv_pii`` of the wrong length when sent (``NZ``), then a test instrument's
  outcome by its fingerprint (World v2: ``51``, ``05``, ``54``, ``GS02``);
  anything else is approved (``0``, ``Approved``). An up-to order refuses a
  payment that would take its payments above its amount;
- a refusal is ``{"status": "error", "errors": [{"description": ...}]}``.

Every request is kept in ``requests`` (operation and body), as the twin's log
would show it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
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

# The twin's refusal texts (services/paynearme logic.py, #2056).
INVALID_CARD_CHECKSUM = "Card number isn’t valid (bad checksum)"
INVALID_ROUTING_CHECKSUM = "ABA isn’t valid (bad checksum)"
INVALID_EXPIRY = "Invalid expiration date"
EXPIRED_CARD = "This card is expired."
#: Declines at charge: (response_code, response_message), as the twin's payments.py has them.
CARD_NUMBER_DECLINE = ("14", "This card number is invalid.")
EXPIRED_CARD_DECLINE = ("54", EXPIRED_CARD)
INVALID_SECURITY_CODE = "This payment didn't complete because the security code was invalid."
INVALID_ACCOUNT_NUMBER = "This account number is invalid"
#: Card-network PAN lengths; an unrecognised prefix may be 12 to 19 digits.
CARD_LENGTHS = {
    "Visa": {13, 16, 19},
    "Mastercard": {16},
    "American Express": {15},
    "Discover": {16, 17, 18, 19},
}


def instrument_fingerprint(kind: str, *parts: Any) -> str:
    """The twin's keyless fingerprint (``logic.instrument_fingerprint``)."""
    normalized = "|".join(re.sub(r"[^0-9A-Za-z/]", "", str(part)) for part in parts)
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


def card_expiry(raw: Any) -> str | None:
    """MM/YYYY for an expiry typed MM/YY, MM/YYYY, MMYY or MMYYYY (the twin's ``card_expiry``)."""
    text = str(raw or "").strip()
    parts = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{2}|\d{4})", text)
    if parts:
        month, year = parts.groups()
    elif re.fullmatch(r"\d{4}|\d{6}", text):
        month, year = text[:2], text[2:]
    else:
        return None
    if not 1 <= int(month) <= 12:
        return None
    return f"{int(month):02d}/{year if len(year) == 4 else f'20{year}'}"


def entered_digits(value: Any) -> str | None:
    """The digits of a number typed with optional spaces or hyphens; None if anything else is in it."""
    text = re.sub(r"[ -]", "", str(value or ""))
    return text if text.isdigit() else None


def card_brand(number: Any) -> str | None:
    digits = re.sub(r"\D", "", str(number))
    if len(digits) < 6:
        return None
    if digits[0] == "4":
        return "Visa"
    if 51 <= int(digits[:2]) <= 55 or 2221 <= int(digits[:4]) <= 2720:
        return "Mastercard"
    if digits[:2] in {"34", "37"}:
        return "American Express"
    if (
        digits.startswith(("6011", "65"))
        or 644 <= int(digits[:3]) <= 649
        or 622126 <= int(digits[:6]) <= 622925
    ):
        return "Discover"
    return None


def luhn_valid(digits: str) -> bool:
    total = 0
    for position, digit in enumerate(int(d) for d in reversed(digits)):
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def card_length_issued(brand: str | None, length: int) -> bool:
    """Whether the brand issues card numbers of this length (an unrecognised prefix: 12 to 19 digits)."""
    return length in CARD_LENGTHS.get(brand or "", set(range(12, 20)))


def card_number_valid(number: Any) -> bool:
    """Whether a card number passes the Luhn checksum (all tokenization checks of it)."""
    digits = entered_digits(number)
    return digits is not None and luhn_valid(digits)


def security_code_valid(code: Any, brand: str | None) -> bool:
    text = str(code or "").strip()
    return text.isdigit() and len(text) == (4 if brand == "American Express" else 3)


def routing_number_valid(routing: Any) -> bool:
    digits = entered_digits(routing)
    if digits is None or len(digits) != 9:
        return False
    d = [int(c) for c in digits]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def account_number_valid(account: Any) -> bool:
    digits = entered_digits(account)
    return digits is not None and 4 <= len(digits) <= 17


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
        self.now = now  # the twin's virtual clock, epoch seconds, while it is frozen
        #: A running clock instead (epoch seconds): a task's sandbox clock starts at its call_time and runs.
        self.clock: Callable[[], int] | None = None
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
            timestamp_ok = abs(int(body.get("timestamp", 0)) - self.virtual_now()) <= WINDOW_S
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

    def virtual_now(self) -> int:
        """The twin's clock now: running when a clock is set, else frozen at ``now``."""
        return self.clock() if self.clock is not None else self.now

    def _decline(self, method: dict[str, Any], security_code: Any) -> tuple[str, str] | None:
        """The twin's declines at charge, in its order (payments.py ``_decline``)."""
        card = method["type"] == "card"
        if card and not card_length_issued(method["brand"], method["length"]):
            return CARD_NUMBER_DECLINE
        if card and self._expired(method["expiry"]):
            return EXPIRED_CARD_DECLINE
        if (
            card
            and security_code not in (None, "")
            and not security_code_valid(security_code, method["brand"])
        ):
            return ("NZ", INVALID_SECURITY_CODE)
        behavior = self.behaviors.get(method["fingerprint"])
        if behavior is not None and behavior["outcome"] in ("decline", "ach_validation_failure"):
            return behavior["response_code"], behavior["response_message"]
        return None

    def _expired(self, expiry: str) -> bool:
        month, year = (int(part) for part in expiry.split("/"))
        today = datetime.fromtimestamp(self.virtual_now(), UTC)
        return (year, month) < (today.year, today.month)

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
            if not card_number_valid(number):
                return _error(INVALID_CARD_CHECKSUM)
            expiry = card_expiry(raw_expiry)
            if expiry is None:
                return _error(INVALID_EXPIRY)
            brand = card_brand(number)
            cvv = body.get("payment_method_cvv_pii")
            if cvv not in (None, "") and not security_code_valid(cvv, brand):
                return _error(INVALID_SECURITY_CODE)
            fingerprint = instrument_fingerprint("card", number, expiry)
            account = {
                "description": "Card",
                "account_type": "Credit",
                "expiration_date": expiry,
                "card_brand": brand,
            }
        else:
            number, aba = body.get("payment_method_ach_account_pii"), body.get("payment_method_ach_aba_pii")
            if not number or not aba:
                return _error("Routing and account numbers are required")
            if not routing_number_valid(aba):
                return _error(INVALID_ROUTING_CHECKSUM)
            if not account_number_valid(number):
                return _error(INVALID_ACCOUNT_NUMBER)
            fingerprint, expiry, brand = instrument_fingerprint("ach", aba, number), None, None
            account = {"description": "Bank Account", "account_type": "Personal Checking"}
        method_id = self._next_id("31")
        last4 = re.sub(r"\D", "", str(number))[-4:]
        account = {
            "payment_method_identifier": method_id,
            "status": "active",
            "name": body.get("payment_method_billing_name", "Account Holder"),
            **{k: v for k, v in account.items() if k not in ("expiration_date", "card_brand")},
            "number": last4,
            **({"expiration_date": expiry, "card_brand": brand} if kind == "card" else {}),
        }
        self.methods[method_id] = {
            "order": order["pnm_order_identifier"],
            "type": kind,
            "fingerprint": fingerprint,
            "expiry": expiry,
            "brand": brand,
            "length": len(entered_digits(number) or "") if kind == "card" else None,
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
        decline = self._decline(method, body.get("cvv_pii"))
        if decline is not None:
            status, (code, message) = "rejected", decline
        else:
            status, code, message = "approved", "0", "Approved"
        payment_id = self._next_id("88")
        self.payments.append(
            {"id": payment_id, "order": order["pnm_order_identifier"], "method": body["payment_method_identifier"],
             "amount_minor": minor, "status": status, "channel": body.get("site_channel")}
        )  # fmt: skip
        account = method["account"]
        return {
            "status": "ok",
            "payment": {  # the twin's payment_json, field for field
                "payment_made": str(self.virtual_now()),
                "payment_amount": _money(minor),
                "payment_currency": "USD",
                "payment_status": status,
                "payment_type": method["type"],
                "payment_account": f"{account['description']} {account['number']}",
                "payment_method_identifier": body["payment_method_identifier"],
                "net_payment_amount": _money(minor),
                "net_payment_currency": "USD",
                "payment_processing_fee": "0.00",
                "payment_processing_fee_currency": "USD",
                "settled_to_site": "false",
                "pnm_payment_identifier": payment_id,
                "site_payment_identifier": body.get("site_payment_identifier"),
                "site_channel": body.get("site_channel"),
                "pnm_order_identifier": order["pnm_order_identifier"],
                "site_order_identifier": order["site_order_identifier"],
                "pnm_customer_identifier": order["pnm_customer_identifier"],
                "site_customer_identifier": order["site_customer_identifier"],
                "response_code": code,
                "response_message": message,
                "return_code": None,
                "refunded_amount": "0.00",
                "refunded_currency": "USD",
            },
        }
