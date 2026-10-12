"""The settlement engine: what Remi answers to a one-time settlement offer, decided in code.

The model only words the decision; it never picks an amount. Per call the engine
holds the amount Remi is asking for (the full balance at first) and how many
counter-offers it has made. On an offer below the ask it counters at most twice,
each time halfway between its ask and the offer, rounded up to the next $5, never
below the settlement floor (``policy.settlement_floor_cents``, one payment). After
two counters it accepts an offer at or above the floor and sends anything less to
a person. ``decide`` is pure: it returns the decision and the next state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_COUNTERS = 2
ROUND_TO_CENTS = 500  # counters are whole $5 amounts, rounded up


@dataclass(frozen=True)
class NegotiationState:
    """One call's negotiation: the amount Remi is asking for and the counters it has made."""

    current_ask_cents: int
    counters_used: int = 0


@dataclass(frozen=True)
class Decision:
    """accept (amount = the offer), counter (amount = the new ask) or transfer (no amount); and the next state."""

    kind: Literal["accept", "counter", "transfer"]
    amount_cents: int | None
    state: NegotiationState


def _midpoint_up(ask_cents: int, offer_cents: int) -> int:
    """Halfway between the ask and the offer, rounded up to the next $5."""
    pair = ask_cents + offer_cents  # the midpoint is pair / 2; round it up to ROUND_TO_CENTS
    return -(-pair // (2 * ROUND_TO_CENTS)) * ROUND_TO_CENTS


def decide(
    state: NegotiationState | None, offer_cents: int, balance_cents: int, floor_cents: int
) -> Decision:
    """The engine's answer to a one-time settlement offer; ``state`` None starts at the full balance."""
    state = state or NegotiationState(current_ask_cents=balance_cents)
    ask = state.current_ask_cents
    if offer_cents >= ask:
        return Decision("accept", offer_cents, state)
    if state.counters_used < MAX_COUNTERS:
        counter = max(floor_cents, min(ask, _midpoint_up(ask, offer_cents)))
        if counter <= offer_cents:
            return Decision("accept", offer_cents, state)
        return Decision("counter", counter, NegotiationState(counter, state.counters_used + 1))
    if offer_cents >= floor_cents:
        return Decision("accept", offer_cents, state)
    return Decision("transfer", None, state)
