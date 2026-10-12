"""The settlement engine as a tool: the consumer's one-time settlement offer in, Remi's answer out.

``negotiation.decide`` picks the answer and its amount; this wrapper reads the
balance from the ledger and the floor from ``policy``, keeps the engine's state
on the call, and tells the model what to say. An accepted amount is the only
one-time settlement ``take_payment`` will charge, once. A transfer goes through
``transfer_to_human``'s own path, so the call's disposition is ``transferred``,
and from then on every collection tool, this one included, is refused (``gates``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import negotiation, policy
from ..call import RemiCall
from ..gates import COLLECTION, Gate
from .catalog import REGISTRY
from .money import DOLLARS, _cents, _open_debt, _refuse_after_charge
from .wrapup import TransferArgs, transfer_to_human


class SettlementOfferArgs(BaseModel):
    offer: float = Field(gt=0, description=f"The consumer's one-time settlement offer. {DOLLARS}")


@REGISTRY.tool(SettlementOfferArgs, requires=Gate.DEBT_UNLOCKED, tags=(COLLECTION,))
async def evaluate_settlement_offer(session: RemiCall, args: SettlementOfferArgs) -> dict[str, Any]:
    """Call on every one-time settlement offer the consumer makes for less than the balance, including when
    they agree to your counter-offer. Decides whether to accept, counter or transfer; say only what it
    returns, and never name any other amount."""
    session.require_consumer()
    account = session.account
    assert account is not None
    balance = (await _open_debt(session)).balance_cents
    _refuse_after_charge(session)  # after the await: nothing below awaits before the state changes
    offer = _cents(args.offer)
    if offer >= balance:
        return {
            "decision": "pay_in_full",
            "amount": policy.dollars(balance),
            "say": f"That covers the full balance, {policy.dollars(balance)}: take it as a payment in full "
            "(take_payment, purpose pay_in_full).",
        }
    floor = policy.settlement_floor_cents(account, balance, 1)
    decision = negotiation.decide(session.negotiation, offer, balance, floor)
    session.negotiation = decision.state
    if decision.kind == "transfer":
        session.settlement_agreed_cents = None
        await transfer_to_human(session, TransferArgs(reason="settlement_below_floor"))
        return {
            "decision": "transfer",
            "say": "This offer can't be accepted on this call: the call is transferred to a person, who takes "
            "it from here. Name no amount. Next: log_contact_attempt and end_call.",
        }
    assert decision.amount_cents is not None
    amount = policy.dollars(decision.amount_cents)
    if decision.kind == "accept":
        session.settlement_agreed_cents = decision.amount_cents
        return {
            "decision": "accept",
            "amount": amount,
            "say": f"Accept {amount} as a one-time settlement. Confirm it with the consumer, then "
            f"add_payment_method and take_payment (purpose settlement, amount {amount}).",
        }
    session.settlement_agreed_cents = None
    return {
        "decision": "counter",
        "amount": amount,
        "say": f"Don't accept {policy.dollars(offer)}. Counter with {amount} as a one-time settlement; name no "
        "other amount.",
    }
