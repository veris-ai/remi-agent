"""Payments through Stripe (the twin the bench names in ``STRIPE_API_BASE``).

``PaymentGateway`` is the seam the tools use; ``StripeGateway`` is the real
one, an instance holding its own ``stripe.StripeClient`` (base URL, key,
timeout), so nothing touches the ``stripe`` module's globals. Tests inject a
fake gateway.

Card numbers, expiry dates, security codes and bank account numbers enter here
and leave only toward Stripe. They are never logged, never returned to the
model, and never written to Postgres; what comes back is a ``SavedMethod``
holding the PaymentMethod id and the last four digits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

import httpx
import stripe

from vamos_voice.tools import ToolError

logger = logging.getLogger(__name__)

STRIPE_TIMEOUT_S = 20
# ACH may report "processing" first; give the twin a moment to settle it.
ACH_SETTLE_POLLS = 3
ACH_SETTLE_DELAY_S = 1.0

Method = Literal["card", "ach"]
T = TypeVar("T")


class PaymentError(ToolError):
    """Stripe refused or could not be reached; the message is safe to show the model.

    A ``ToolError``, so the framework's dispatch hands it to the model as
    ``{"error": "The payment processor refused: ..."}`` and the call goes on.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"The payment processor refused: {detail}")


@dataclass(frozen=True)
class CardDetails:
    number: str = field(repr=False)
    exp_month: int = field(repr=False)
    exp_year: int = field(repr=False)
    cvc: str = field(repr=False)


@dataclass(frozen=True)
class BankDetails:
    routing_number: str = field(repr=False)
    account_number: str = field(repr=False)


@dataclass(frozen=True)
class SavedMethod:
    """A payment method attached to the account's Stripe customer."""

    payment_method_id: str
    method: Method
    last4: str
    brand: str | None = None


@dataclass(frozen=True)
class ChargeResult:
    payment_intent_id: str
    status: Literal["succeeded", "failed"]
    decline_message: str | None = None


class PaymentGateway(Protocol):
    """What Remi's payment tools need from a processor."""

    async def create_customer(self, *, account_id: str, name: str) -> str: ...

    async def save_card(self, *, customer_id: str, name: str, card: CardDetails) -> SavedMethod: ...

    async def save_bank_account(self, *, customer_id: str, name: str, bank: BankDetails) -> SavedMethod: ...

    async def charge(
        self, *, customer_id: str, method: SavedMethod, amount_cents: int, account_id: str
    ) -> ChargeResult: ...


class _TwinAwareHTTPX(stripe.HTTPXClient):
    """stripe's async httpx client, trusting ``SSL_CERT_FILE`` when set.

    The SDK pins its own CA bundle, so a twin served under a cluster CA would
    fail TLS. Pinned to stripe<17, whose ``HTTPXClient`` keeps its async client
    on ``_client_async``; tests/test_payments.py covers the override.
    """

    def __init__(self, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(timeout=timeout)
        cafile = os.environ.get("SSL_CERT_FILE") or stripe.ca_bundle_path
        self._client_async = httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=cafile), transport=transport
        )


class StripeGateway:
    """Stripe via an instance-scoped ``StripeClient``."""

    def __init__(
        self, api_key: str, api_base: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        base = {"api": api_base.rstrip("/")} if api_base else None
        self._client = stripe.StripeClient(
            api_key,
            base_addresses=base,
            http_client=_TwinAwareHTTPX(STRIPE_TIMEOUT_S, transport),
            max_network_retries=1,
        )

    async def create_customer(self, *, account_id: str, name: str) -> str:
        customer = await self._call(
            self._client.v1.customers.create_async({"name": name, "metadata": {"account_id": account_id}})
        )
        return customer.id

    async def save_card(self, *, customer_id: str, name: str, card: CardDetails) -> SavedMethod:
        pm = await self._call(
            self._client.v1.payment_methods.create_async(
                {
                    "type": "card",
                    "card": {
                        "number": card.number,
                        "exp_month": card.exp_month,
                        "exp_year": card.exp_year,
                        "cvc": card.cvc,
                    },
                    "billing_details": {"name": name},
                }
            )
        )
        await self._call(self._client.v1.payment_methods.attach_async(pm.id, {"customer": customer_id}))
        return SavedMethod(pm.id, "card", pm.card.last4, pm.card.brand)

    async def save_bank_account(self, *, customer_id: str, name: str, bank: BankDetails) -> SavedMethod:
        pm = await self._call(
            self._client.v1.payment_methods.create_async(
                {
                    "type": "us_bank_account",
                    "us_bank_account": {
                        "account_holder_type": "individual",
                        "routing_number": bank.routing_number,
                        "account_number": bank.account_number,
                    },
                    "billing_details": {"name": name},
                }
            )
        )
        await self._call(self._client.v1.payment_methods.attach_async(pm.id, {"customer": customer_id}))
        return SavedMethod(pm.id, "ach", pm.us_bank_account.last4)

    async def charge(
        self, *, customer_id: str, method: SavedMethod, amount_cents: int, account_id: str
    ) -> ChargeResult:
        params: dict[str, object] = {
            "amount": amount_cents,
            "currency": "usd",
            "customer": customer_id,
            "payment_method": method.payment_method_id,
            "payment_method_types": ["card" if method.method == "card" else "us_bank_account"],
            "confirm": True,
            "metadata": {"account_id": account_id},
        }
        if method.method == "ach":
            # Authorized on a recorded call: an offline mandate.
            params["mandate_data"] = {"customer_acceptance": {"type": "offline"}}
        try:
            intent = await self._client.v1.payment_intents.create_async(params)
        except stripe.CardError as err:
            failed = (err.error.payment_intent or {}) if err.error else {}
            intent_id = failed.get("id") if isinstance(failed, dict) else getattr(failed, "id", None)
            if not intent_id:
                raise PaymentError(
                    f"the card was declined before a payment was created: {err.user_message}"
                ) from err
            return ChargeResult(intent_id, "failed", err.user_message or "declined")
        except stripe.StripeError as err:
            raise PaymentError(_safe(err)) from err
        intent = await self._settle(intent)
        if intent.status == "succeeded":
            return ChargeResult(intent.id, "succeeded")
        if intent.status == "processing":
            # ACH settles after the call; Stripe accepted the debit. Recorded as succeeded
            # because payments.status has no "processing" (WORLD.md).
            logger.warning("payment %s still processing after polling; recording it as succeeded", intent.id)
            return ChargeResult(intent.id, "succeeded")
        error = getattr(intent, "last_payment_error", None)
        return ChargeResult(intent.id, "failed", getattr(error, "message", None) or intent.status)

    async def _settle(self, intent: stripe.PaymentIntent) -> stripe.PaymentIntent:
        for _ in range(ACH_SETTLE_POLLS):
            if intent.status != "processing":
                break
            await asyncio.sleep(ACH_SETTLE_DELAY_S)
            intent = await self._call(self._client.v1.payment_intents.retrieve_async(intent.id))
        return intent

    @staticmethod
    async def _call(awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except stripe.StripeError as err:
            raise PaymentError(_safe(err)) from err


def _safe(err: stripe.StripeError) -> str:
    """Stripe's user-facing message: never echoes request parameters (card data)."""
    return err.user_message or f"Stripe refused the request ({type(err).__name__})"
