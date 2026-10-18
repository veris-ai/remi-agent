"""Test support: a real Postgres loaded with the World's own SQL (schema, payment gateway, rows).

Postgres comes from ``REMI_TEST_DATABASE_URL`` when set, otherwise from a
throwaway ``postgres:16-alpine`` container started with the docker CLI. With
neither available the database tests are skipped with that reason; there is
no fake database and no fake gateway (the SQL is the thing under test).

The World's SQL is vendored in ``fixtures/collections_world.sql`` (its header
names the benchmark repo commit), and every account's money, which the World
keeps in its Fineract ledger, in ``fixtures/collections_ledger.json``: seeding
also rebuilds ``LEDGER``, the in-memory ledger (``fake_ledger.FakeLedger``) a
test's calls post to. The DSN the fixtures hand out is the owner's,
for seeding and inspecting; Remi itself connects as ``APP_ROLE``, a login that
owns nothing, so the gateway's write guards bind: a direct write to
``payment_methods`` or ``payments``, or any read of the processor's vault,
fails the test.

The World's SQL includes the creditors' decision engine (``payment_options``,
``request_approval`` and the guards that refuse off-menu money). ``seed_world``
gives its account ``TEST_POLICY`` (override fields with ``policy=``), priced
from the ledger's balance as the World prices it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .fake_ledger import FakeLedger

FIXTURES = Path(__file__).parent / "fixtures"
WORLD_SQL = (FIXTURES / "collections_world.sql").read_text()
#: Every account's money as the World's ledger holds it (account id -> the FakeLedger.add_account arguments).
LEDGER_ACCOUNTS: dict[str, dict[str, Any]] = json.loads((FIXTURES / "collections_ledger.json").read_text())[
    "accounts"
]
ROWS_MARKER = "-- ===== collections_bench/generated/world/rows.sql ====="
#: The schema and the gateway (the database), and the World's ~110 accounts (its rows).
DATABASE_SQL, ROWS_SQL = WORLD_SQL.split(ROWS_MARKER)
CALL_TIME = "2027-02-10T09:00:00-05:00"
CONTEXT_JSON = json.dumps({"account_id": "TAS-420101", "call_time": CALL_TIME, "phone_id": "ph_420101_1"})

APP_ROLE, APP_PASSWORD = "remi_app", "remi_app"

# The gateway's test instruments (WORLD.md, "Outcomes"): Stripe's published test numbers.
GOOD_CARD = "4242424242424242"
AMEX_CARD = "378282246310005"
INSUFFICIENT_FUNDS_CARD = "4000000000009995"
DECLINED_CARD = "4000000000000002"
EXPIRED_CARD = "4000000000000069"
GOOD_BANK = ("110000000", "000123456789")
DECLINING_BANK = ("110000000", "000222222227")


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
    "sol_expires_on": date(2028, 3, 15),
    "validation_notice_mailed_on": date(2027, 1, 5),
    "time_barred_notice_mailed_on": None,
    "policy_id": "pol_test",
    "status": "active",
}
#: TAS-420101's money, in the ledger: what ``seed_world``'s ``account`` overrides of these keys change.
MONEY = {
    "charge_off_on": date(2025, 3, 15),
    "charge_off_balance_cents": 18500,
    "interest_since_charge_off_cents": 0,
    "fees_since_charge_off_cents": 0,
    "paid_since_charge_off_cents": 0,
    "last_payment_on": None,
}
#: The creditor policy seeded accounts get: plans of 3 to 6 months with no down payment, then settlement
#: tiers of 10 and 20% off, and 30% off with a supervisor's approval, each only after a recorded hardship.
TEST_POLICY: dict[str, Any] = {
    "policy_id": "pol_test",
    "name": "Remi test ladder",
    "plan_min_months": 3,
    "plan_max_months": [{"balance_up_to_cents": None, "months": 6}],
    "plan_down_pct": 0,
    "plan_min_monthly_cents": 2500,
    "settlement_allowed": True,
    "settlement_requires_hardship": True,
    "settlement_tiers": [
        {"discount_pct": 10, "requires_approval": False},
        {"discount_pct": 20, "requires_approval": False},
        {"discount_pct": 30, "requires_approval": True},
    ],
    "settlement_due_within_days": 7,
    "rules": [
        "Offer the options in the order listed. Move to the next only if the consumer turns down the one before.",
        "A settlement only after the consumer has told you, in their own words, about a hardship.",
        "If nothing fits, transfer to a person.",
    ],
}
#: The ledger every call in a test reaches; rebuilt by each seed.
LEDGER = FakeLedger()

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


def _jsonb(row: dict[str, Any]) -> dict[str, Any]:
    return {k: Jsonb(v) if isinstance(v, list) else v for k, v in row.items()}


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
    policy: dict[str, Any] | None = None,
) -> None:
    """Reset the database (schema and gateway) and seed one agency, one creditor and one account
    (TAS-420101 by default, as the World has it), with the given overrides; the overrides of ``MONEY``'s
    keys (and a ``current_balance_cents``, as a charge-off balance with nothing else) seed the ledger. The
    account's creditor policy is ``TEST_POLICY`` with ``policy``'s fields, and its ``placed_balance_cents``
    (what the menu is priced from) the ledger's balance unless overridden."""
    overrides = dict(account or {})
    money = {**MONEY, **{k: overrides.pop(k) for k in list(overrides) if k in MONEY}}
    if "current_balance_cents" in overrides:
        money["charge_off_balance_cents"] = overrides.pop("current_balance_cents")
    _reset_ledger({(ACCOUNT | overrides)["account_id"]: money})
    with psycopg.connect(dsn, autocommit=True) as conn:
        _reset(conn)
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
        _insert(conn, "creditor_policies", _jsonb({**TEST_POLICY, **(policy or {})}))
        account_id = (ACCOUNT | overrides)["account_id"]
        _insert(
            conn, "accounts", {**ACCOUNT, "placed_balance_cents": LEDGER.balance(account_id), **overrides}
        )
        for row in LETTER_HISTORY if history is None else history:
            _insert(conn, "contact_attempts", row)
        _grant_app(conn)


def seed_full_world(dsn: str) -> None:
    """Reset and load the whole World as the bench seeds it: schema, gateway, every row, and every
    account's money in the ledger."""
    _reset_ledger(
        {
            account_id: {
                **money,
                "charge_off_on": date.fromisoformat(money["charge_off_on"]),
                "last_payment_on": money["last_payment_on"] and date.fromisoformat(money["last_payment_on"]),
            }
            for account_id, money in LEDGER_ACCOUNTS.items()
        }
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        _reset(conn)
        conn.execute(ROWS_SQL)
        _grant_app(conn)


def _reset_ledger(accounts: dict[str, dict[str, Any]]) -> None:
    global LEDGER
    LEDGER = FakeLedger()
    for account_id, money in accounts.items():
        LEDGER.add_account(account_id, **money)


def _reset(conn: psycopg.Connection) -> None:
    conn.execute("drop schema public cascade; create schema public;")
    conn.execute(DATABASE_SQL)


def _grant_app(conn: psycopg.Connection) -> None:
    """The app login Remi uses: the agency's tables, the three gateway functions, nothing of the processor's."""
    conn.execute(
        f"""
        do $$ begin
          if not exists (select from pg_roles where rolname = '{APP_ROLE}') then
            create role {APP_ROLE} login password '{APP_PASSWORD}';
          end if;
        end $$;
        grant usage on schema public to {APP_ROLE};
        grant select, insert, update, delete on all tables in schema public to {APP_ROLE};
        revoke all on public.processor_vault from {APP_ROLE};
        """
    )
    helpers = conn.execute(
        "select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.proname like 'processor%'"
    ).fetchall()
    for (signature,) in helpers:
        conn.execute(f"revoke execute on function {signature} from public")


def app_dsn(owner_dsn: str) -> str:
    """The same database, as the app login Remi connects with."""
    return make_conninfo(owner_dsn, user=APP_ROLE, password=APP_PASSWORD)


def rows(dsn: str, table: str, where: str = "true") -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(f"select * from {table} where {where}").fetchall()


def all_text(dsn: str) -> str:
    """Every value in every table, as text: for asserting card data never landed in Postgres."""
    with psycopg.connect(dsn) as conn:
        tables = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
        return "\n".join(str(conn.execute(f"select * from {t}").fetchall()) for t in tables)


Snapshot = dict[str, dict[Any, dict[str, Any]]]


def snapshot(dsn: str) -> Snapshot:
    """Every row of every table, keyed by table then by the row's first column (its primary key)."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        tables = [
            r["tablename"]
            for r in conn.execute("select tablename from pg_tables where schemaname = 'public' order by 1")
        ]
        result: Snapshot = {}
        for table in tables:
            fetched = conn.execute(f"select * from {table}").fetchall()
            result[table] = {next(iter(row.values())): row for row in fetched}
        return result


def changes(before: Snapshot, after: Snapshot) -> list[dict[str, Any]]:
    """The answer key's shape: ``{"op": "insert", "table", "row"}`` and ``{"op": "update", "table", "key",
    "set"}`` (only the changed columns). Deletes are reported too, so a test sees them."""
    found: list[dict[str, Any]] = []
    for table, now in after.items():
        then = before.get(table, {})
        for key, row in now.items():
            if key not in then:
                found.append({"op": "insert", "table": table, "row": row})
            elif row != then[key]:
                changed = {c: v for c, v in row.items() if then[key].get(c) != v}
                found.append({"op": "update", "table": table, "key": key, "set": changed})
        found.extend({"op": "delete", "table": table, "key": k} for k in then.keys() - now.keys())
    return found
