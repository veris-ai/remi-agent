"""The creditor's policy for this account, and the supervisor.

``get_payment_options`` returns the World's ``creditor_policy(account_id,
call_date)`` verbatim (World v2 WORLD.md, "Creditor policies"): how the creditor
is named, the balance, the options (``pif``, ``payment_plan`` and each
``discount_<pct>``) with their amounts, terms and due dates, the agency's offer
rules and the creditor's own rules. It is fixed policy and amounts priced from
the account; nothing in it depends on what has happened on the call, and Remi
adds, drops and rewords nothing.

``request_approval`` asks the World's supervisor (a person in the World) for an
OK and returns only the answer: ``{"approved": bool, "reason": str}``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..call import RemiCall
from .catalog import REGISTRY


class NoArgs(BaseModel):
    pass


@REGISTRY.tool(NoArgs)
async def get_payment_options(session: RemiCall, _: NoArgs) -> dict[str, Any]:
    """This account's creditor policy: how the creditor is named, the balance, what may be offered and on what
    terms (the full balance 'pif', a 'payment_plan', each 'discount_<pct>', with their amounts and due dates),
    and the creditor's other rules."""
    return await session.read_policy()


class ApprovalArgs(BaseModel):
    option_id: str = Field(description="The option to ask a supervisor's OK for, e.g. 'discount_40'.")


@REGISTRY.tool(ApprovalArgs)
async def request_approval(session: RemiCall, args: ApprovalArgs) -> dict[str, Any]:
    """Asks a supervisor for an OK on one of this account's options. Returns the supervisor's answer."""
    answer = await session.db.policy_function(
        "request_approval", {"account_id": session.context.account_id, "option_id": args.option_id}
    )
    return {"approved": bool(answer["approved"]), "reason": str(answer["reason"])}
