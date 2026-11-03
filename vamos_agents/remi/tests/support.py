"""Test support: a real Postgres loaded with World v2's own SQL, its ledger and its PayNearMe, as test doubles.

Postgres comes from ``REMI_TEST_DATABASE_URL`` when set, otherwise from a
throwaway ``postgres:16-alpine`` container started with the docker CLI. With
neither available the database tests are skipped with that reason; there is
no fake database (the World's SQL, its policy engine included, is under test).

The World is vendored in ``fixtures/``: ``collections_world.sql`` (schema, the
creditors' policy engine, rows: its header names the benchmark repo commit) and
``world_v2_manifest.json`` (the Fineract loans, the PayNearMe orders and test
instruments, each pilot task's hand-off). ``seed_world`` loads the whole World
and rebuilds ``LEDGER`` (``fake_ledger.FakeLedger``, the World's Fineract as
Remi relies on it) and ``PAYNEARME`` (``fake_paynearme.FakePayNearMe``, the
twin's vendor API) from the manifest. The DSN the fixtures hand out is the
owner's, for seeding and inspecting; Remi itself connects as ``APP_ROLE``, a
login that owns nothing, as the deployed agent role does not own the policy
engine's record either.
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

from vamos_agents.remi.context import CallContext

from .fake_ledger import FakeLedger
from .fake_paynearme import FakePayNearMe

FIXTURES = Path(__file__).parent / "fixtures"
WORLD_SQL = (FIXTURES / "collections_world.sql").read_text()
ROWS_MARKER = "-- ===== collections_bench/world_v2/generated/world/rows.sql ====="
#: The schema and the policy engine (the database), and the twelve pilot accounts (its rows).
DATABASE_SQL, ROWS_SQL = WORLD_SQL.split(ROWS_MARKER)
MANIFEST: dict[str, Any] = json.loads((FIXTURES / "world_v2_manifest.json").read_text())
#: The reviewed policy tool's output for each pilot task's account: get_payment_options' oracle.
PAYMENT_OPTIONS = {p.stem: p.read_text() for p in sorted((FIXTURES / "payment_options").glob("*.json"))}
CALL_DATE = date.fromisoformat(MANIFEST["call_date"])
#: Each pilot task's hand-off, as the bench stamps it.
CONTEXTS: dict[str, dict[str, str]] = MANIFEST["agent_contexts"]
TASK_OF_ACCOUNT = {c["account_id"]: task for task, c in CONTEXTS.items()}
CONTEXT_JSON = json.dumps(CONTEXTS["CARD-04"])

APP_ROLE, APP_PASSWORD = "remi_app", "remi_app"

# World v1's test instruments, on PayNearMe (World v2 WORLD.md, "Payments: PayNearMe").
GOOD_CARD = ("4111111111111111", 8, 2028, "123")
INSUFFICIENT_FUNDS_CARD = ("4000000000009995", 11, 2029, "123")
DECLINED_CARD = ("4000000000000002", 10, 2028, "123")
EXPIRED_CARD = ("4000000000000069", 11, 2031, "123")
GOOD_BANK = ("110000000", "000123456789")
INVALID_BANK = ("110000000", "000222222227")


def context(task: str) -> CallContext:
    """A pilot task's hand-off, parsed as Remi parses it."""
    raw = CONTEXTS[task]
    return CallContext(raw["account_id"], raw["phone_id"], datetime.fromisoformat(raw["call_time"]))


def account_id(task: str) -> str:
    return CONTEXTS[task]["account_id"]


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

#: The World's ledger and PayNearMe every call in a test reaches; rebuilt by each seed.
LEDGER = FakeLedger.from_manifest(MANIFEST)
PAYNEARME = FakePayNearMe.from_manifest(MANIFEST)


def seed_world(dsn: str, *, sql: str = "") -> None:
    """Reset the database and load World v2 as the bench seeds it (schema, policy engine, rows), then
    ``sql`` (a test's own change to the World, run as the owner); rebuild the ledger and PayNearMe."""
    global LEDGER, PAYNEARME
    LEDGER = FakeLedger.from_manifest(MANIFEST)
    PAYNEARME = FakePayNearMe.from_manifest(MANIFEST)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("drop schema public cascade; create schema public;")
        conn.execute(DATABASE_SQL)
        conn.execute(ROWS_SQL)
        if sql:
            conn.execute(sql)
        _grant_app(conn)


def _grant_app(conn: psycopg.Connection) -> None:
    """The app login Remi uses: the agency's tables, and the World's functions (executable by anyone)."""
    conn.execute(
        f"""
        do $$ begin
          if not exists (select from pg_roles where rolname = '{APP_ROLE}') then
            create role {APP_ROLE} login password '{APP_PASSWORD}';
          end if;
        end $$;
        grant usage on schema public to {APP_ROLE};
        grant select, insert, update, delete on all tables in schema public to {APP_ROLE};
        """
    )


def app_dsn(owner_dsn: str) -> str:
    """The same database, as the app login Remi connects with."""
    return make_conninfo(owner_dsn, user=APP_ROLE, password=APP_PASSWORD)


def rows(dsn: str, table: str, where: str = "true") -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(f"select * from {table} where {where}").fetchall()


def compliance(dsn: str, action: str | None = None) -> list[dict[str, Any]]:
    """The policy engine's record, read as the owner (a superuser, past row-level security), in order."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        found = conn.execute(
            "select account_id, action, subject, rule, passed, detail from policy_compliance order by check_id"
        ).fetchall()
    return [r for r in found if action is None or r["action"] == action]


def all_text(dsn: str) -> str:
    """Every value in every table, as text: for asserting card data never landed in Postgres."""
    with psycopg.connect(dsn) as conn:
        tables = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
        return "\n".join(str(conn.execute(f"select * from {t}").fetchall()) for t in tables)


Snapshot = dict[str, dict[Any, dict[str, Any]]]


def snapshot(dsn: str) -> Snapshot:
    """Every row of every table but the policy engine's record, keyed by table then by the row's first
    column (its primary key)."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        tables = [
            r["tablename"]
            for r in conn.execute(
                "select tablename from pg_tables where schemaname = 'public' "
                "and tablename <> 'policy_compliance' order by 1"
            )
        ]
        result: Snapshot = {}
        for table in tables:
            fetched = conn.execute(f"select * from {table}").fetchall()
            result[table] = {next(iter(row.values())): row for row in fetched}
        return result


def changes(before: Snapshot, after: Snapshot) -> list[dict[str, Any]]:
    """``{"op": "insert", "table", "row"}`` and ``{"op": "update", "table", "key", "set"}`` (only the
    changed columns). Deletes are reported too, so a test sees them."""
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
