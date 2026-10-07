"""Typed rows of the collections world (WORLD.md's schema), as Remi reads them.

Plain frozen dataclasses: the policy reasons over these, the tools return
selected fields of them to the model, and nothing here touches the database.
Secrets (date of birth, ZIP, SSN last four, street) live on ``Consumer`` so the
verifier can compare against them; ``get_account`` never returns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

CLOSED_STATUSES = ("cease", "attorney", "bankruptcy", "deceased", "disputed", "recalled")


@dataclass(frozen=True)
class Agency:
    """The one ``agency`` row: who Remi works for and the human callback line."""

    name: str
    dcwp_license_number: str
    callback_number: str
    callback_contact: str


@dataclass(frozen=True)
class Creditor:
    creditor_id: str
    name: str
    kind: str
    mailing_address: str | None
    financial_assistance_policy: bool


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
    language_preference: str | None
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
    placed_on: date
    charge_off_on: date
    charge_off_balance_cents: int
    interest_since_charge_off_cents: int
    fees_since_charge_off_cents: int
    paid_since_charge_off_cents: int
    current_balance_cents: int
    last_payment_on: date | None
    sol_expires_on: date
    validation_notice_mailed_on: date
    time_barred_notice_mailed_on: date | None
    settlement_floor_pct: int
    max_plan_months: int
    min_instalment_cents: int
    status: str
