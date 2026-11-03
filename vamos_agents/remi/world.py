"""Typed rows of the collections world (World v2's schema, WORLD.md), as Remi reads them.

Plain frozen dataclasses: the tools return selected fields of them to the model,
and nothing here touches the database. An account's money is not here: it is
the account's loan in the ledger (``ledger.Debt``). The creditor's policy is not
here either: the World prices it for an account (``creditor_policy``).
Secrets (date of birth, ZIP, SSN last four, street) live on ``Consumer`` so the
verifier can compare against them; no tool returns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Agency:
    """The one ``agency`` row: who Remi works for and the human callback line."""

    name: str
    dcwp_license_number: str
    callback_number: str
    callback_contact: str

    @property
    def business_name(self) -> str:
        """The name as said on a call ("Tallmere Account Services"), without the legal suffix."""
        return self.name.removesuffix(", LLC")


@dataclass(frozen=True)
class Creditor:
    creditor_id: str
    name: str
    kind: str  # bank_card, medical, telecom, retail, government, auto
    store_name: str | None  # a store card's store; None for any other creditor
    mailing_address: str | None
    financial_assistance_policy: bool
    policy_id: str


@dataclass(frozen=True)
class Consumer:
    consumer_id: str
    first_name: str
    last_name: str
    date_of_birth: date
    ssn_last4: str
    street: str
    city: str
    zip: str
    deceased_on: date | None
    estate_rep_name: str | None
    estate_rep_phone: str | None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class Phone:
    phone_id: str
    consumer_id: str
    number: str
    line_type: str
    source: str
    artificial_voice_consent: bool
    consent_revoked_at: datetime | None
    status: str


@dataclass(frozen=True)
class ContactAttempt:
    attempt_id: str
    at: datetime
    medium: str
    direction: str
    answered_by: str | None
    consumer_responded: bool
    disposition: str | None
    summary: str | None


@dataclass(frozen=True)
class Account:
    account_id: str
    consumer_id: str
    creditor_id: str
    original_account_last4: str
    placed_on: date  # the loan's date: placement (hospital), sale (auto), charge-off (others)
    sol_expires_on: date | None  # None: no time to sue on file (phone accounts)
    validation_notice_mailed_on: date
    time_barred_notice_mailed_on: date | None
    placed_balance_cents: int
    pnm_order_identifier: str  # the account's PayNearMe order
    balance_phone_cents: int | None  # a phone account's balance in its two parts (both or neither)
    balance_service_cents: int | None
    treatment_on: date | None  # a hospital account's date of treatment
    car: str | None  # an auto deficiency: the car, all five facts or none
    car_repossessed_on: date | None
    car_sold_on: date | None
    car_sale_price_cents: int | None
    deficiency_explanation_sent_on: date | None
    status: str
