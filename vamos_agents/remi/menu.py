"""The creditor's payment menu: the World's decision engine (WORLD.md, "Payment options").

A real collections platform does not let the collector invent terms: a
decision engine applies the creditor's rules to the account and answers an
approved menu, and the collector picks from it and frames it. The World has
that engine as two SQL functions in the collections database, and this module
is Remi's one client for them:

- ``payment_options(account_id)`` answers the ordered ladder: pay in full, the
  plans, then (if the creditor allows them) the settlement tiers, lowest
  discount first, some needing a supervisor's approval;
- ``request_approval(account_id, option_id)`` asks for that approval and logs
  the answer (approved only when a hardship is recorded on the account).

The money tools take an option id, never a free amount, and ``checked_option``
is the code gate every one of them applies before it writes or charges anything. The
World refuses off-menu writes too (an error whose message starts ``policy:``);
``PolicyRefused`` carries that refusal to the model, never hidden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from vamos_voice.tools import ToolError

from .db import Database, Transaction

logger = logging.getLogger(__name__)

OptionKind = Literal["pay_in_full", "plan", "settlement"]
PAY_IN_FULL = "pif"
POLICY_ERROR_PREFIX = "policy:"
TO_A_PERSON = "Anything outside the creditor's options goes to a person (transfer_to_human)."


class MenuUnavailable(ToolError):
    """The payment options could not be read or made no sense: nothing may be offered but a person."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"The creditor's payment options cannot be read ({detail}). {TO_A_PERSON}")


class PolicyRefused(ToolError):
    """The World refused a write as outside the creditor's policy; the message is the World's own."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"The collections system refused it as outside the creditor's policy: {detail}. {TO_A_PERSON}"
        )


@dataclass(frozen=True)
class Option:
    """One rung of the ladder, as ``payment_options`` answers it (cents, as the World stores them)."""

    id: str
    kind: OptionKind
    amount_cents: int  # what the option pays in all (a plan: the balance)
    months: int | None = None
    down_cents: int = 0
    monthly_cents: int | None = None
    discount_pct: int | None = None  # never shown to the model
    requires_approval: bool = False
    due_within_days: int | None = None


@dataclass(frozen=True)
class Menu:
    """The creditor's approved options for one account, in the order they are offered."""

    creditor: str
    balance_cents: int
    settlement_allowed: bool
    settlement_requires_hardship: bool
    options: tuple[Option, ...]

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.id == option_id), None)


def _option(raw: dict[str, Any], balance_cents: int) -> Option:
    kind = raw["kind"]
    if kind == "pay_in_full":
        return Option(raw["id"], kind, int(raw.get("amount_cents", balance_cents)))
    if kind == "plan":
        return Option(
            raw["id"],
            kind,
            balance_cents,
            months=int(raw["months"]),
            down_cents=int(raw.get("down_cents") or 0),
            monthly_cents=int(raw["monthly_cents"]),
        )
    if kind == "settlement":
        return Option(
            raw["id"],
            kind,
            int(raw["amount_cents"]),
            discount_pct=int(raw["discount_pct"]),
            requires_approval=bool(raw.get("requires_approval", False)),
            due_within_days=int(raw["due_within_days"]),
        )
    raise ValueError(f"unknown option kind {kind!r}")


def parse_menu(raw: Any) -> Menu:
    """``payment_options``' jsonb as a ``Menu``; ValueError when it does not follow the contract."""
    try:
        balance = int(raw["balance_cents"])
        return Menu(
            creditor=str(raw["creditor"]),
            balance_cents=balance,
            settlement_allowed=bool(raw["settlement_allowed"]),
            settlement_requires_hardship=bool(raw["settlement_requires_hardship"]),
            options=tuple(_option(o, balance) for o in raw["options"]),
        )
    except (KeyError, TypeError) as err:
        raise ValueError(f"malformed payment options: {err!r}") from err


@dataclass(frozen=True)
class Approval:
    approved: bool
    reason: str


#: An option the rules let Remi offer or take now (``option_status``).
AVAILABLE = "available"


def option_status(menu: Menu, option: Option, *, hardship_recorded: bool, approvals: dict[str, bool]) -> str:
    """``AVAILABLE``, or what the creditor's rules still need before this option may be offered or taken."""
    if option.kind != "settlement":
        return AVAILABLE
    if not menu.settlement_allowed:
        return "not offered: this creditor allows no settlement"
    if menu.settlement_requires_hardship and not hardship_recorded:
        return "locked: a settlement needs the consumer's hardship recorded first (record_flag hardship)"
    if option.requires_approval:
        decided = approvals.get(option.id)
        if decided is None:
            return "locked: needs a supervisor's approval first (request_approval)"
        if not decided:
            return "not offered: approval was denied"
    return AVAILABLE


def checked_option(
    menu: Menu, option_id: str, *, hardship_recorded: bool, approvals: dict[str, bool]
) -> Option:
    """The option, if the creditor's rules let it be taken now; otherwise a ``ToolError`` saying why."""
    option = menu.option(option_id)
    if option is None:
        raise ToolError(
            f"'{option_id}' is not one of the creditor's options (get_payment_options). {TO_A_PERSON}"
        )
    status = option_status(menu, option, hardship_recorded=hardship_recorded, approvals=approvals)
    if status != AVAILABLE:
        raise ToolError(f"Option {option_id} is {status}. Nothing was written or charged.")
    return option


async def payment_options(db: Database, account_id: str) -> Menu:
    """The account's menu now. Unreadable or malformed is logged and refused (``MenuUnavailable``)."""
    try:
        raw = await db.policy_function("payment_options", {"account_id": account_id})
        return parse_menu(raw)
    except (psycopg.Error, ValueError) as err:
        logger.error("payment_options(%s) failed: %s", account_id, err)
        raise MenuUnavailable("the decision engine did not answer") from err


async def request_approval(db: Database, account_id: str, option_id: str) -> Approval:
    """Ask the World for a supervisor's approval of an approval tier; the World logs the answer."""
    try:
        raw = await db.policy_function("request_approval", {"account_id": account_id, "option_id": option_id})
        return Approval(bool(raw["approved"]), str(raw.get("reason") or ""))
    except (psycopg.Error, KeyError, TypeError) as err:
        logger.error("request_approval(%s, %s) failed: %s", account_id, option_id, err)
        raise MenuUnavailable("the approval request did not go through") from err


async def guarded_insert(tx: Transaction, table: str, row: dict[str, Any]) -> None:
    """``tx.insert``, with the World's policy refusal (``policy: ...``) raised as ``PolicyRefused``."""
    try:
        await tx.insert(table, row)
    except psycopg.Error as err:
        message = (err.diag.message_primary or "") if err.diag else ""
        if not message.startswith(POLICY_ERROR_PREFIX):
            raise
        logger.error("the World refused %s on %s: %s", table, row.get("account_id"), message)
        raise PolicyRefused(message.removeprefix(POLICY_ERROR_PREFIX).strip()) from err
