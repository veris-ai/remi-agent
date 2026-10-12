"""Payments through the collections system's gateway (WORLD.md, "The payment gateway").

Stripe does not serve debt collection agencies, so the World takes payments the
way a real agency does: inside its collections platform, whose integrated
processor tokenizes the card or bank account and authorizes the charge. Until a
twin of such a processor exists it lives in the collections Postgres as three
SQL functions, and ``PaymentGateway`` is Remi's one client for them:

- ``gateway_tokenize_card`` / ``gateway_tokenize_ach`` save an instrument on the
  account and answer its ``method_id`` (the existing one for a card or bank
  account already saved there);
- ``gateway_charge`` charges a saved method and writes the ``payments`` row.
  It does not know the balance: posting an approved charge to the account's
  loan is the ledger's (``ledger.FineractLedger.post_payment``).

The world decides approve or decline; a decline is a normal result, not an
error. ``gateway_charge`` first checks the amount against the creditor's menu:
an off-menu amount raises SQLSTATE 23514 (``policy: ...``) before any payment
row, raised here as ``menu.PolicyRefused``. An invalid instrument, unknown method or out-of-range amount raises
SQLSTATE 22023 with a message starting ``gateway:``; nothing is written, and
the message becomes a ``ToolError`` the model can relay.

Remi never writes ``payment_methods`` or ``payments`` itself, never touches the
processor's ``processor_vault`` or ``processor_*`` helpers, and runs no DDL.
Card numbers, expiry dates, security codes and bank account numbers pass
through here only as gateway arguments: never logged, never returned to the
model, never written anywhere else. ``at`` is always the call's clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import psycopg

from vamos_voice.tools import ToolError

from .db import Transaction
from .menu import raise_policy_refusal

logger = logging.getLogger(__name__)

Kind = Literal["card", "ach"]
DeclineCode = Literal["insufficient_funds", "card_declined", "expired_card"]

GATEWAY_ERROR_PREFIX = "gateway:"


class PaymentError(ToolError):
    """The gateway refused the request (SQLSTATE 22023); the message is the gateway's own words.

    A ``ToolError``, so the framework's dispatch hands it to the model as
    ``{"error": "The payment gateway refused it: ..."}`` and the call goes on.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"The payment gateway refused it: {detail}")


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
    """A card or bank account the gateway tokenized on the account (a ``payment_methods`` row)."""

    method_id: str
    kind: Kind
    last4: str
    brand: str | None = None


@dataclass(frozen=True)
class Charge:
    """One ``gateway_charge`` answer: the ``payments`` row it wrote."""

    payment_id: str
    status: Literal["approved", "declined"]
    decline_code: DeclineCode | None
    processor_ref: str

    @property
    def approved(self) -> bool:
        return self.status == "approved"


class PaymentGateway:
    """The gateway functions, called inside the tool's own transaction on the collections database."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    async def tokenize_card(
        self, *, account_id: str, holder_name: str, card: CardDetails, at: datetime
    ) -> SavedMethod:
        row = await self._call(
            "gateway_tokenize_card",
            account_id=account_id,
            pan=card.number,
            exp_month=card.exp_month,
            exp_year=card.exp_year,
            cvc=card.cvc,
            holder_name=holder_name,
            at=at,
        )
        return SavedMethod(row["method_id"], "card", row["last4"].strip(), row["brand"])

    async def tokenize_ach(
        self, *, account_id: str, holder_name: str, bank: BankDetails, at: datetime
    ) -> SavedMethod:
        row = await self._call(
            "gateway_tokenize_ach",
            account_id=account_id,
            routing_number=bank.routing_number,
            account_number=bank.account_number,
            holder_name=holder_name,
            at=at,
        )
        return SavedMethod(row["method_id"], "ach", row["last4"].strip())

    async def charge(self, *, method_id: str, amount_cents: int, at: datetime) -> Charge:
        row = await self._call("gateway_charge", method_id=method_id, amount_cents=amount_cents, at=at)
        return Charge(row["payment_id"], row["status"], row["decline_code"], row["processor_ref"])

    async def _call(self, function: str, **args: object) -> dict[str, object]:
        try:
            return await self._tx.call_gateway(function, args)
        except psycopg.errors.CheckViolation as err:  # SQLSTATE 23514: the creditor's menu refused the amount
            raise_policy_refusal(err, function)
            raise
        except psycopg.errors.InvalidParameterValue as err:  # SQLSTATE 22023
            message = err.diag.message_primary or ""
            if not message.startswith(GATEWAY_ERROR_PREFIX):
                raise
            # The gateway's own words: they never echo the card or account number.
            raise PaymentError(message.removeprefix(GATEWAY_ERROR_PREFIX).strip()) from err
