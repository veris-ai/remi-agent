"""Test support: a real Postgres with WORLD.md's schema, a seeded account, a fake Stripe.

Postgres comes from ``REMI_TEST_DATABASE_URL`` when set, otherwise from a
throwaway ``postgres:16-alpine`` container started with the docker CLI. With
neither available the database tests are skipped with that reason; there is
no fake database (the SQL is the thing under test).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from vamos_agents.remi.payments import BankDetails, CardDetails, ChargeResult, SavedMethod

SCHEMA = (Path(__file__).parent / "fixtures" / "world_schema.sql").read_text()
CALL_TIME = "2027-02-10T09:00:00-05:00"
CONTEXT_JSON = json.dumps({"account_id": "TAS-420101", "call_time": CALL_TIME, "phone_id": "ph_420101_1"})

GOOD_CARD = "4242424242424242"
DECLINING_CARD = "4000000000009995"
GOOD_BANK = ("110000000", "000123456789")
FAILING_BANK = ("110000000", "000111111116")


class PostgresUnavailable(RuntimeError):
    pass


def start_postgres() -> tuple[str, str | None]:
    """A DSN and, when this started one, the container to remove afterwards."""
    if dsn := os.environ.get("REMI_TEST_DATABASE_URL"):
        return dsn, None
    if not shutil.which("docker"):
        raise PostgresUnavailable("no docker CLI and no REMI_TEST_DATABASE_URL")
    name = f"remi-test-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=remi",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if run.returncode != 0:
        raise PostgresUnavailable(f"docker run failed: {run.stderr.strip()[:300]}")
    port = subprocess.run(
        ["docker", "port", name, "5432"], capture_output=True, text=True, timeout=30, check=True
    )
    host_port = port.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
    dsn = f"postgresql://postgres:remi@127.0.0.1:{host_port}/postgres"
    deadline = time.monotonic() + 60
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("select 1")
            return dsn, name
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
                raise PostgresUnavailable("postgres container never became ready") from None
            time.sleep(0.5)


def stop_postgres(container: str | None) -> None:
    if container:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60, check=False)


# --- seeding ----------------------------------------------------------------------------

ACCOUNT = {
    "account_id": "TAS-420101",
    "consumer_id": "con_420101",
    "creditor_id": "cr_ashcombe",
    "original_account_last4": "4001",
    "placed_on": date(2027, 1, 4),
    "charge_off_on": date(2025, 3, 15),
    "charge_off_balance_cents": 18500,
    "interest_since_charge_off_cents": 0,
    "fees_since_charge_off_cents": 0,
    "paid_since_charge_off_cents": 0,
    "current_balance_cents": 18500,
    "last_payment_on": None,
    "sol_expires_on": date(2028, 3, 15),
    "validation_notice_mailed_on": date(2027, 1, 5),
    "time_barred_notice_mailed_on": None,
    "settlement_floor_pct": 70,
    "max_plan_months": 12,
    "min_instalment_cents": 2500,
    "status": "active",
    "stripe_customer_id": None,
}
CONSUMER = {
    "consumer_id": "con_420101",
    "first_name": "Aaliyah",
    "last_name": "Alvarez",
    "date_of_birth": date(1958, 1, 1),
    "ssn_last4": "2234",
    "street": "100 7th Ave",
    "city": "Brooklyn",
    "zip": "11215",
    "language_preference": None,
    "deceased_on": None,
    "estate_rep_name": None,
    "estate_rep_phone": None,
}
PHONE = {
    "phone_id": "ph_420101_1",
    "consumer_id": "con_420101",
    "number": "+17185550100",
    "line_type": "mobile",
    "source": "consumer",
    "artificial_voice_consent": True,
    "consent_revoked_at": None,
    "status": "valid",
}
LETTER_HISTORY = [
    {
        "attempt_id": "att_420101_1",
        "account_id": "TAS-420101",
        "phone_id": None,
        "at": datetime.fromisoformat("2027-01-05T10:00:00-05:00"),
        "medium": "letter",
        "direction": "outbound",
        "answered_by": None,
        "consumer_responded": False,
        "duration_s": None,
        "disposition": None,
        "summary": "Validation notice mailed.",
        "agent_name": None,
    }
]


def _insert(conn: psycopg.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"insert into {table} ({cols}) values ({marks})", list(row.values()))


def seed_world(
    dsn: str,
    *,
    account: dict[str, Any] | None = None,
    consumer: dict[str, Any] | None = None,
    phone: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    creditor_address: str | None = "PO Box 4410, Wilmington, DE 19850",
    creditor_kind: str = "bank_card",
) -> None:
    """Reset the schema and seed one agency, one creditor and one account (TAS-420101 by default)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("drop schema public cascade; create schema public;")
        conn.execute(SCHEMA)
        _insert(
            conn,
            "agency",
            {
                "name": "Tallmere Account Services, LLC",
                "dcwp_license_number": "2091843-DCA",
                "callback_number": "+12125550100",
                "callback_contact": "Dana Whitlock",
            },
        )
        _insert(
            conn,
            "creditors",
            {
                "creditor_id": "cr_ashcombe",
                "name": "Ashcombe Bank, N.A.",
                "kind": creditor_kind,
                "mailing_address": creditor_address,
                "financial_assistance_policy": False,
            },
        )
        _insert(conn, "consumers", {**CONSUMER, **(consumer or {})})
        _insert(conn, "phones", {**PHONE, **(phone or {})})
        _insert(conn, "accounts", {**ACCOUNT, **(account or {})})
        for row in LETTER_HISTORY if history is None else history:
            _insert(conn, "contact_attempts", row)


def rows(dsn: str, table: str, where: str = "true") -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(f"select * from {table} where {where}").fetchall()


def all_text(dsn: str) -> str:
    """Every value in every table, as text: for asserting card data never landed in Postgres."""
    with psycopg.connect(dsn) as conn:
        tables = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
        return "\n".join(str(conn.execute(f"select * from {t}").fetchall()) for t in tables)


# --- a fake Stripe ------------------------------------------------------------------------


@dataclass
class FakePayments:
    """Stripe's documented test-card behaviour, in memory: 9995 declines, 000111111116 fails."""

    customers: list[str] = field(default_factory=list)
    charges: list[dict[str, Any]] = field(default_factory=list)
    _secrets: dict[str, str] = field(default_factory=dict)

    async def create_customer(self, *, account_id: str, name: str) -> str:
        customer = f"cus_test_{len(self.customers) + 1}"
        self.customers.append(customer)
        return customer

    async def save_card(self, *, customer_id: str, name: str, card: CardDetails) -> SavedMethod:
        pm = f"pm_card_{len(self._secrets) + 1}"
        self._secrets[pm] = card.number
        return SavedMethod(pm, "card", card.number[-4:], "visa")

    async def save_bank_account(self, *, customer_id: str, name: str, bank: BankDetails) -> SavedMethod:
        pm = f"pm_bank_{len(self._secrets) + 1}"
        self._secrets[pm] = bank.account_number
        return SavedMethod(pm, "ach", bank.account_number[-4:])

    async def charge(
        self, *, customer_id: str, method: SavedMethod, amount_cents: int, account_id: str
    ) -> ChargeResult:
        secret = self._secrets[method.payment_method_id]
        ok = secret not in (DECLINING_CARD, FAILING_BANK[1])
        intent = f"pi_test_{len(self.charges) + 1}"
        self.charges.append(
            {"method": method.method, "amount_cents": amount_cents, "status": "succeeded" if ok else "failed"}
        )
        return ChargeResult(
            intent, "succeeded" if ok else "failed", None if ok else "Your card has insufficient funds."
        )
