"""Payments on PayNearMe (World v2 WORLD.md, "Payments: PayNearMe"): tokenize, charge, report.

Stripe does not serve debt collection agencies; PayNearMe does. Each placed
account has one PayNearMe ``up-to`` order of its placed balance
(``accounts.pnm_order_identifier``), and ``PayNearMe`` is Remi's one client for
the vendor's JSON API v3.0 on the World's twin:

- ``add_card`` / ``add_bank_account``: ``create_payment_method`` on the order,
  answering its ``payment_method_identifier`` and last four digits;
- ``pay``: ``make_payment`` on the order, answering PayNearMe's payment
  (``pnm_payment_identifier``, ``payment_status``, ``net_payment_amount``,
  ``response_code``, ``response_message``).

PayNearMe decides approve or decline, and a decline is an answer, not an error.
Nothing here checks the creditor's policy: PayNearMe charges what it is sent,
subject only to its own rules (its declines, the up-to order's limit, its error
envelope, which is raised as ``PayNearMeRefused`` in PayNearMe's own words). An
unreachable or failing twin is ``PaymentsUnavailable``.

Every request carries ``version`` 3.0, the site id, a ``timestamp`` and a
``signature``: HMAC-SHA256 with the site's secret over each parameter's name and
value, sorted by name, PayNearMe's excluded names left out. The twin refuses a
timestamp more than 300 seconds from its virtual clock, so ``at`` is always the
call's clock (``CallClock``: the World's time), never wall time.

Card numbers, expiry dates, security codes and bank account numbers pass
through here only as PayNearMe's ``_pii`` parameters: never logged, never kept,
never returned. The billing details (name, address, ZIP, phone) are what the
agent gave, passed as given and never filled in from the account record. The
HMAC secret is never in a repr.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import httpx

from vamos_voice.tools import ToolError

logger = logging.getLogger(__name__)

API_VERSION = "3.0"
API_PATH = "/json-api"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
#: The parameter names PayNearMe leaves out of the signature.
UNSIGNED = frozenset(
    {"signature", "call", "action", "controller", "format", "fp", "print_buttons", "datafile", "api",
     "business_document", "bank_document", "return_url", "afo", "app", "site_id", "iframe", "locale", "x", "y"}
)  # fmt: skip
#: How payments taken on a call are marked (``site_channel``).
SITE_CHANNEL = "agent"

Kind = Literal["card", "ach"]


class PayNearMeRefused(ToolError):
    """PayNearMe refused the request (its error envelope); the message is PayNearMe's own words."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"PayNearMe refused it: {detail}")
        self.detail = detail


class PaymentsUnavailable(RuntimeError):
    """PayNearMe could not be reached, or failed (an HTTP error status)."""


@dataclass(frozen=True)
class SavedMethod:
    """A card or bank account PayNearMe tokenized on the account's order: never its number."""

    method_id: str  # PayNearMe's payment_method_identifier
    kind: Kind
    last4: str
    brand: str | None


@dataclass(frozen=True)
class Payment:
    """PayNearMe's answer to a payment, as the agency records it."""

    payment_id: str  # PayNearMe's pnm_payment_identifier
    status: str  # approved, rejected
    amount_cents: int  # net_payment_amount: what goes towards the balance
    response_code: str | None
    response_message: str | None
    method_id: str
    kind: Kind
    last4: str

    @property
    def approved(self) -> bool:
        return self.status == "approved"


class _Secret:
    """The HMAC secret, kept out of every repr."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<secret>"


def sign(params: dict[str, str], secret: str) -> str:
    """PayNearMe's request signature: HMAC-SHA256 over ``name + value`` for each signed parameter, sorted."""
    canonical = "".join(f"{name}{params[name]}" for name in sorted(params) if name not in UNSIGNED)
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def dollars(cents: int) -> str:
    """PayNearMe's amount: major units, two decimals ("1440.00")."""
    return f"{Decimal(cents) / 100:.2f}"


def to_cents(amount: Any) -> int:
    return int((Decimal(str(amount)) * 100).to_integral_value())


def _digits(value: str | None) -> str | None:
    return re.sub(r"\D", "", value) if value is not None else None


def _billing(
    name: str | None, address: str | None, zipcode: str | None, phone: str | None
) -> dict[str, str | None]:
    """PayNearMe's billing parameters, as given (a card needs the name, address and ZIP; a bank account the name
    and ZIP; the phone is optional)."""
    return {
        "payment_method_billing_name": name,
        "payment_method_billing_address": address,
        "payment_method_billing_zipcode": zipcode,
        "payment_method_billing_phone": phone,
    }


def _given(params: dict[str, str | None]) -> dict[str, str]:
    """The parameters that were given: one not given is not sent, so PayNearMe names it if it requires it."""
    return {name: value for name, value in params.items() if value is not None}


class PayNearMe:
    """The World's PayNearMe over HTTP. One per pod; every request with a timeout."""

    def __init__(
        self,
        base_url: str,
        site_identifier: str,
        hmac_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = base_url.rstrip("/").removesuffix(API_PATH)
        self._http = httpx.AsyncClient(base_url=base + API_PATH, timeout=TIMEOUT, transport=transport)
        self.site_identifier = site_identifier
        self._secret = _Secret(hmac_secret)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, operation: str, params: dict[str, str], at: datetime) -> dict[str, Any]:
        """One signed request, timestamped with ``at`` (the World's time); PayNearMe's ``ok`` answer."""
        body = {
            "version": API_VERSION,
            "site_identifier": self.site_identifier,
            "timestamp": str(int(at.timestamp())),
            **params,
        }
        body["signature"] = sign(body, self._secret.value)
        try:
            resp = await self._http.post(f"/{operation}", json=body)
        except httpx.HTTPError as err:
            raise PaymentsUnavailable(f"PayNearMe could not be reached: {type(err).__name__}") from err
        if resp.status_code >= 400:
            raise PaymentsUnavailable(f"PayNearMe failed ({resp.status_code}) on {operation}")
        answer = resp.json()
        if answer.get("status") != "ok":
            errors = answer.get("errors") or [{}]
            detail = "; ".join(str(e.get("description") or "error") for e in errors)
            logger.warning("PayNearMe refused %s: %s", operation, detail)
            raise PayNearMeRefused(detail)
        return answer

    async def add_card(
        self,
        *,
        order: str,
        number: str | None = None,
        exp_month: int | None = None,
        exp_year: int | None = None,
        cvc: str | None = None,
        billing_name: str | None = None,
        billing_address: str | None = None,
        billing_zip: str | None = None,
        billing_phone: str | None = None,
        at: datetime,
    ) -> SavedMethod:
        """Tokenize a card with what was given; PayNearMe names anything it requires that is missing."""
        expiry = f"{exp_month:02d}/{exp_year}" if exp_month is not None and exp_year is not None else None
        params = {
            "pnm_order_identifier": order,
            "payment_method_type": "card",
            "payment_method_card_number_pii": _digits(number),
            "payment_method_card_expiry_pii": expiry,
            "payment_method_cvv_pii": _digits(cvc),
            **_billing(billing_name, billing_address, billing_zip, billing_phone),
        }
        return _saved(await self._call("create_payment_method", _given(params), at), "card")

    async def add_bank_account(
        self,
        *,
        order: str,
        routing: str | None = None,
        account: str | None = None,
        billing_name: str | None = None,
        billing_address: str | None = None,
        billing_zip: str | None = None,
        billing_phone: str | None = None,
        at: datetime,
    ) -> SavedMethod:
        """Tokenize a bank account with what was given; PayNearMe names anything it requires that is missing."""
        params = {
            "pnm_order_identifier": order,
            "payment_method_type": "ach",
            "payment_method_ach_aba_pii": _digits(routing),
            "payment_method_ach_account_pii": _digits(account),
            **_billing(billing_name, billing_address, billing_zip, billing_phone),
        }
        return _saved(await self._call("create_payment_method", _given(params), at), "ach")

    async def pay(self, *, order: str, method: SavedMethod, amount_cents: int, at: datetime) -> Payment:
        answer = await self._call(
            "make_payment",
            {
                "pnm_order_identifier": order,
                "payment_method_identifier": method.method_id,
                "payment_amount": dollars(amount_cents),
                "payment_currency": "USD",
                "site_channel": SITE_CHANNEL,
            },
            at,
        )
        paid = answer["payment"]
        return Payment(
            payment_id=str(paid["pnm_payment_identifier"]),
            status=str(paid["payment_status"]),
            amount_cents=to_cents(paid["net_payment_amount"]),
            response_code=paid.get("response_code"),
            response_message=paid.get("response_message"),
            method_id=method.method_id,
            kind=method.kind,
            last4=method.last4,
        )


def _saved(answer: dict[str, Any], kind: Kind) -> SavedMethod:
    """The one method ``create_payment_method`` answers (only the new one is in its answer)."""
    account = answer["order"]["electronic_payments"]["payment_methods"][0]["accounts"][0]
    return SavedMethod(
        method_id=str(account["payment_method_identifier"]),
        kind=kind,
        last4=str(account["number"]),
        brand=account.get("card_brand"),
    )
