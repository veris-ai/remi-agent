"""The collections database (WORLD.md's Postgres), reached with psycopg 3.

One ``Database`` per pod (one call per pod), opened by ``RemiAgent.connect`` and handed to each ``RemiCall``.
Every connection carries a connect timeout and a statement timeout, so a stuck
twin turns into a tool error instead of dead air. Calls are serialized with a
lock: the session's tools share one connection and one account.

Writes go through ``Transaction.insert``/``update`` with table and column names
checked against WORLD.md's schema and quoted as identifiers, so no value is
ever spliced into SQL. ``payment_methods`` and ``payments`` are not writable:
only the World's gateway functions write them, reached through
``Transaction.call_gateway`` (see ``payments``). Timestamps are always passed in
from ``CallClock``; nothing here calls ``now()``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .world import Account, Agency, Consumer, ContactAttempt, Creditor, Phone

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5_000

#: The columns Remi may write, per table (WORLD.md). Anything else is a bug.
WRITABLE: dict[str, frozenset[str]] = {
    # A payment's effect on the balance, paid-to-date and last payment date is the gateway's to write.
    "accounts": frozenset({"status"}),
    "consumers": frozenset({"deceased_on"}),  # English-only: never language_preference
    "phones": frozenset({"consent_revoked_at", "status"}),
    "contact_attempts": frozenset(
        {
            "attempt_id",
            "account_id",
            "phone_id",
            "at",
            "medium",
            "direction",
            "answered_by",
            "consumer_responded",
            "duration_s",
            "disposition",
            "summary",
            "agent_name",
        }
    ),
    "account_flags": frozenset({"flag_id", "account_id", "kind", "details", "created_at"}),
    "arrangements": frozenset(
        {
            "arrangement_id",
            "account_id",
            "kind",
            "total_cents",
            "instalments",
            "method_id",
            "created_at",
            "confirmation_letter_due_by",
        }
    ),
    "promises_to_pay": frozenset(
        {"promise_id", "account_id", "amount_cents", "due_on", "method", "created_at"}
    ),
    "callbacks": frozenset({"callback_id", "account_id", "phone_id", "scheduled_for", "consumer_requested"}),
    "letters": frozenset({"letter_id", "account_id", "kind", "queued_at"}),
}
KEYS = {
    "accounts": "account_id",
    "consumers": "consumer_id",
    "phones": "phone_id",
    "account_flags": "flag_id",
}
JSON_COLUMNS = frozenset({"details", "instalments"})
#: WORLD.md's payment gateway: the only way a payment method or payment is written.
GATEWAY_FUNCTIONS = frozenset({"gateway_tokenize_card", "gateway_tokenize_ach", "gateway_charge"})


class NotFound(LookupError):
    """A row the call context names does not exist."""


def _check(table: str, columns: set[str]) -> None:
    allowed = WRITABLE.get(table)
    if allowed is None or not columns <= allowed:
        raise ValueError(f"Remi does not write {sorted(columns - (allowed or set()))} on {table}")


def _value(column: str, value: Any) -> Any:
    return Jsonb(value) if column in JSON_COLUMNS else value


class Transaction:
    """Writes that land together or not at all."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def insert(self, table: str, row: dict[str, Any]) -> None:
        _check(table, set(row))
        query = sql.SQL("insert into {} ({}) values ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        )
        await self._conn.execute(query, [_value(c, v) for c, v in row.items()])

    async def update(self, table: str, key: str, changes: dict[str, Any]) -> None:
        _check(table, set(changes))
        query = sql.SQL("update {} set {} where {} = %s").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in changes),
            sql.Identifier(KEYS[table]),
        )
        cursor = await self._conn.execute(query, [*(_value(c, v) for c, v in changes.items()), key])
        if cursor.rowcount != 1:
            raise NotFound(f"{table} {key} matched {cursor.rowcount} rows")

    async def call_gateway(self, function: str, args: dict[str, Any]) -> dict[str, Any]:
        """``select * from <gateway function>(name => value, ...)``: its one result row.

        Only the World's three payment-gateway functions; their parameter names are
        identifiers and every value is a bound parameter. Errors propagate (``payments`` maps them).
        """
        if function not in GATEWAY_FUNCTIONS:
            raise ValueError(f"{function} is not a payment-gateway function")
        query = sql.SQL("select * from {}({})").format(
            sql.Identifier("public", function),
            sql.SQL(", ").join(sql.SQL("{} => %s").format(sql.Identifier(name)) for name in args),
        )
        cursor = await self._conn.execute(query, list(args.values()))
        row = await cursor.fetchone()
        if row is None:
            raise NotFound(f"{function} answered no row")
        return row


class Database:
    """The collections database for this call. Construct with ``connect``."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(cls, dsn: str) -> Database:
        conn = await psycopg.AsyncConnection.connect(
            dsn,
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_S,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            row_factory=dict_row,
        )
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def ping(self) -> None:
        async with self._lock:
            await self._conn.execute("select 1")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self._lock, self._conn.transaction():
            yield Transaction(self._conn)

    async def _one(self, query: str, params: tuple[Any, ...], what: str) -> dict[str, Any]:
        async with self._lock:
            cursor = await self._conn.execute(query, params)
            row = await cursor.fetchone()
        if row is None:
            raise NotFound(what)
        return row

    async def _all(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._conn.execute(query, params)
            return await cursor.fetchall()

    # --- reads ---------------------------------------------------------------------------

    async def agency(self) -> Agency:
        row = await self._one(
            "select name, dcwp_license_number, callback_number, callback_contact from agency limit 1",
            (),
            "agency row",
        )
        return Agency(**row)

    async def account(self, account_id: str) -> Account:
        row = await self._one(
            f"select {', '.join(Account.__dataclass_fields__)} from accounts where account_id = %s",
            (account_id,),
            f"account {account_id}",
        )
        return Account(**row)

    async def consumer(self, consumer_id: str) -> Consumer:
        row = await self._one(
            f"select {', '.join(Consumer.__dataclass_fields__)} from consumers where consumer_id = %s",
            (consumer_id,),
            f"consumer {consumer_id}",
        )
        return Consumer(**{**row, "ssn_last4": row["ssn_last4"].strip(), "zip": row["zip"].strip()})

    async def creditor(self, creditor_id: str) -> Creditor:
        row = await self._one(
            f"select {', '.join(Creditor.__dataclass_fields__)} from creditors where creditor_id = %s",
            (creditor_id,),
            f"creditor {creditor_id}",
        )
        return Creditor(**row)

    async def phone(self, phone_id: str) -> Phone:
        row = await self._one(
            f"select {', '.join(Phone.__dataclass_fields__)} from phones where phone_id = %s",
            (phone_id,),
            f"phone {phone_id}",
        )
        return Phone(**row)

    async def phones(self, consumer_id: str) -> list[Phone]:
        rows = await self._all(
            f"select {', '.join(Phone.__dataclass_fields__)} from phones where consumer_id = %s order by phone_id",
            (consumer_id,),
        )
        return [Phone(**r) for r in rows]

    async def contact_history(self, account_id: str) -> list[ContactAttempt]:
        rows = await self._all(
            f"select {', '.join(ContactAttempt.__dataclass_fields__)} from contact_attempts "
            "where account_id = %s order by at",
            (account_id,),
        )
        return [ContactAttempt(**r) for r in rows]

    async def flag_details(self, flag_id: str) -> dict[str, Any]:
        row = await self._one(
            "select details from account_flags where flag_id = %s", (flag_id,), f"flag {flag_id}"
        )
        return row["details"]
