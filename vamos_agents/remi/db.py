"""The collections database (WORLD.md's Postgres), reached with psycopg 3.

One ``Database`` per pod (one call per pod), opened by ``RemiAgent.connect`` and handed to each ``RemiCall``.
Every connection carries a connect timeout and a statement timeout, so a stuck
twin turns into a tool error instead of dead air. Calls are serialized with a
lock: the session's tools share one connection and one account.

Writes go through ``Transaction.insert``/``update`` with table and column names
checked against World v2's schema and quoted as identifiers, so no value is
ever spliced into SQL. ``payments`` is the agency's record of a PayNearMe
payment, written by the agent after PayNearMe answered (see ``payments``). The
creditor's policy for an account and a supervisor's answer are the World's two
functions, ``creditor_policy`` and ``request_approval``, reached through
``policy_function``. Timestamps are always passed in from ``CallClock``;
nothing here calls ``now()``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
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
    # The money (balance, payments) is the ledger's: an account row records only where collection stands.
    "accounts": frozenset({"status"}),
    "consumers": frozenset({"deceased_on"}),
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
    # The agency's record of a PayNearMe payment it took (World v2): PayNearMe's ids and answer.
    "payments": frozenset(
        {
            "payment_id",
            "account_id",
            "method_id",
            "amount_cents",
            "status",
            "response_code",
            "response_message",
            "payment_type",
            "last4",
            "at",
        }
    ),
}
KEYS = {
    "accounts": "account_id",
    "consumers": "consumer_id",
    "phones": "phone_id",
    "account_flags": "flag_id",
}
JSON_COLUMNS = frozenset({"details", "instalments"})
#: The World's policy functions: an account's creditor policy, priced, and a supervisor's answer.
POLICY_FUNCTIONS = frozenset({"creditor_policy", "request_approval"})


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

    async def policy_function(self, function: str, args: dict[str, Any]) -> Any:
        """``select <policy function>(name => value, ...)``: its answer, parsed (``creditor_policy``'s json
        keeps its key order).

        Only the World's two policy functions; names are identifiers and every value is bound. Errors
        propagate."""
        if function not in POLICY_FUNCTIONS:
            raise ValueError(f"{function} is not one of the World's policy functions")
        query = sql.SQL("select {}({}) as answer").format(
            sql.Identifier("public", function),
            sql.SQL(", ").join(sql.SQL("{} => %s").format(sql.Identifier(name)) for name in args),
        )
        cursor = await self._conn.execute(query, list(args.values()))
        row = await cursor.fetchone()
        if row is None or row["answer"] is None:
            raise NotFound(f"{function} answered nothing")
        return row["answer"]


async def _open(dsn: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        row_factory=dict_row,
    )


def _lost(conn: psycopg.AsyncConnection) -> bool:
    """The connection is gone (the server or the network closed it), not merely an SQL error."""
    return conn.closed or conn.broken


class Database:
    """The collections database for this call. Construct with ``connect``.

    A connection the server or the network drops is opened again: before the next statement when the
    drop is already known, or once more when a read or a transaction's ``BEGIN`` finds it gone (nothing
    ran, so nothing runs twice). A drop in the middle of a transaction is never retried: what committed is
    unknown, so the error reaches the tool and the next statement runs on a fresh connection."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        reopen: Callable[[], Awaitable[psycopg.AsyncConnection]] | None = None,
    ) -> None:
        self._conn = conn
        self._reopen = reopen
        self._lock = asyncio.Lock()
        self._closing = False

    @classmethod
    async def connect(cls, dsn: str) -> Database:
        return cls(await _open(dsn), reopen=lambda: _open(dsn))

    async def close(self) -> None:
        self._closing = True
        await self._conn.close()

    async def _live(self) -> psycopg.AsyncConnection:
        """The connection, opened again first if it was lost (callers hold the lock)."""
        if _lost(self._conn) and self._reopen is not None and not self._closing:
            logger.warning("collections database connection lost; reconnecting")
            await self._conn.close()
            self._conn = await self._reopen()
        return self._conn

    async def _once_more_if_lost[T](self, attempt: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
        """Run ``attempt`` on the live connection; if the connection turns out to be gone, run it once more
        on a new one. Only for work that leaves nothing behind when the connection fails under it."""
        conn = await self._live()
        try:
            return await attempt(conn)
        except psycopg.OperationalError:
            if not _lost(conn) or self._reopen is None:
                raise
        return await attempt(await self._live())

    async def ping(self) -> None:
        async with self._lock:
            await self._once_more_if_lost(lambda conn: conn.execute("select 1"))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self._lock, AsyncExitStack() as stack:

            async def begin(conn: psycopg.AsyncConnection) -> psycopg.AsyncConnection:
                await stack.enter_async_context(conn.transaction())
                return conn

            conn = await self._once_more_if_lost(begin)
            yield Transaction(conn)

    async def policy_function(self, function: str, args: dict[str, Any]) -> Any:
        """One policy-function call in its own transaction (``request_approval`` logs the answer)."""
        async with self.transaction() as tx:
            return await tx.policy_function(function, args)

    async def _one(self, query: str, params: tuple[Any, ...], what: str) -> dict[str, Any]:
        async def read(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()

        async with self._lock:
            row = await self._once_more_if_lost(read)
        if row is None:
            raise NotFound(what)
        return row

    async def _all(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async def read(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()

        async with self._lock:
            return await self._once_more_if_lost(read)

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
