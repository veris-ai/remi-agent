"""The creditor's payment menu: what may be offered, and the supervisor's approval for a tier.

``get_payment_options`` answers the World's decision engine (``menu``): the
ordered ladder the creditor approved for this account, shown in dollars, with
each option's id (what the money tools take) and, where the rules still need
something first (a recorded hardship, a supervisor's approval), its status.
Percentages and the engine's limits are never shown: the model can only say
amounts an option carries. ``request_approval`` asks for an approval tier.

Both need the debt unlocked and are refused once collection has stopped.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from vamos_voice.tools import ToolError

from .. import menu as engine
from .. import policy
from ..call import RemiCall
from ..gates import COLLECTION, Gate
from ..ledger import Debt
from .catalog import REGISTRY

logger = logging.getLogger(__name__)

LADDER = (
    "The creditor's options, in the order they are offered: the full balance, then each plan, then a settlement "
    "only when no plan fits and the consumer has explained a hardship (recorded), lowest discount first. Only "
    "these amounts and terms exist: never a percentage, a minimum, or an option before it is reached. An option "
    "with a status is not offered until it is available. Anything else goes to a person (transfer_to_human)."
)
NO_SETTLEMENT = (
    "This creditor allows no settlement: a consumer who can pay none of these options goes to a person "
    "(transfer_to_human)."
)


async def current_menu(session: RemiCall, debt: Debt) -> engine.Menu:
    """The menu now, refused (and logged) when, before the call's first charge, its balance is not the
    ledger's: its amounts would be wrong. The World prices it from the balance at the start of the call and
    does not reprice it after a payment, so once something was charged the two differ by design."""
    menu = await session.read_menu()
    if session.charged_payment_id is None and menu.balance_cents != debt.balance_cents:
        logger.error(
            "payment options for %s price a balance of %d cents; the ledger holds %d",
            session.context.account_id,
            menu.balance_cents,
            debt.balance_cents,
        )
        raise engine.MenuUnavailable("they do not match the account's current balance")
    return menu


def status(session: RemiCall, menu: engine.Menu, option: engine.Option) -> str:
    return engine.option_status(
        menu, option, hardship_recorded=session.hardship_recorded, approvals=session.approvals
    )


def shown(session: RemiCall, menu: engine.Menu, option: engine.Option) -> dict[str, Any]:
    """One option as the model sees it: dollars, terms, a pay-by date; never a percentage."""
    usd = policy.dollars
    out: dict[str, Any] = {"option_id": option.id, "kind": option.kind}
    if option.kind == "plan":
        assert option.months is not None and option.monthly_cents is not None
        out["months"] = option.months
        if option.down_cents:
            out["down_payment_today"] = usd(option.down_cents)
        out["monthly"] = usd(option.monthly_cents)
    else:
        out["amount"] = usd(option.amount_cents)
    if option.due_within_days is not None:
        out["pay_by"] = (session.clock.today() + timedelta(days=option.due_within_days)).isoformat()
    if (state := status(session, menu, option)) != engine.AVAILABLE:
        out["status"] = state
    return out


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def get_payment_options(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """The creditor's approved payment options for this account, in the order to offer them: pay in full, plans,
    then any settlement tiers. Offer only these, by their amounts and terms; pass an option's option_id to
    take_payment, create_arrangement or record_promise. Anything not listed goes to a person."""
    session.require_consumer()
    debt = await session.read_debt()
    menu = await current_menu(session, debt)
    result: dict[str, Any] = {
        "balance": policy.dollars(menu.balance_cents),
        "options": [shown(session, menu, o) for o in menu.options],
        "allowed_now": LADDER,
    }
    if not menu.settlement_allowed:
        result["settlement"] = NO_SETTLEMENT
    return result


class ApprovalArgs(BaseModel):
    option_id: str = Field(description="The settlement option that needs a supervisor's approval.")


@REGISTRY.tool(ApprovalArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def request_approval(session: RemiCall, args: ApprovalArgs) -> dict[str, Any]:
    """Ask a supervisor to approve a settlement tier whose status says it needs approval, before offering it.
    If approval is denied, that option is never offered on this call."""
    session.require_consumer()
    debt = await session.read_debt()
    menu = await current_menu(session, debt)
    option = menu.option(args.option_id)
    if option is None or not option.requires_approval:
        raise ToolError(f"'{args.option_id}' is not an option that needs approval (get_payment_options).")
    if args.option_id not in session.approvals:
        if not menu.settlement_allowed or (
            menu.settlement_requires_hardship and not session.hardship_recorded
        ):
            raise ToolError(
                f"Option {option.id} is {status(session, menu, option)}; no approval was asked for."
            )
        answer = await engine.request_approval(session.db, session.context.account_id, option.id)
        session.approvals[option.id] = answer.approved
        reason = answer.reason
    else:
        reason = "already decided on this call"
    if session.approvals[option.id]:
        return {
            "option_id": option.id,
            "approved": True,
            "reason": reason,
            "option": shown(session, menu, option),
            "allowed_now": "This option may now be offered, at its amount and pay-by date only.",
        }
    return {
        "option_id": option.id,
        "approved": False,
        "reason": reason,
        "allowed_now": "This option is not offered on this call. If no available option fits, a person can "
        "handle it (transfer_to_human).",
    }
