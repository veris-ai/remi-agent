"""The settlement engine (``negotiation.decide``): pure decisions, no database."""

from __future__ import annotations

from vamos_agents.remi.negotiation import NegotiationState, decide

BALANCE = 200_000  # $2,000.00
FLOOR = 140_000  # $1,400.00


def _run(*offers_dollars: int) -> list[tuple[str, int | None]]:
    state: NegotiationState | None = None
    out = []
    for dollars in offers_dollars:
        decision = decide(state, dollars * 100, BALANCE, FLOOR)
        state = decision.state
        out.append((decision.kind, decision.amount_cents))
    return out


def test_two_counters_at_the_rounded_midpoint_then_accept_above_the_floor() -> None:
    assert _run(1500, 1600, 1620) == [("counter", 175_000), ("counter", 167_500), ("accept", 162_000)]


def test_counters_never_go_below_the_floor_and_less_goes_to_a_person() -> None:
    assert _run(1000, 1100, 1200) == [("counter", 150_000), ("counter", 140_000), ("transfer", None)]


def test_an_offer_at_or_above_the_ask_is_accepted() -> None:
    assert _run(2000) == [("accept", 200_000)]
    assert _run(2500) == [("accept", 250_000)]
    assert _run(1500, 1750) == [("counter", 175_000), ("accept", 175_000)]


def test_a_counter_never_exceeds_the_ask() -> None:
    # mid(2000, 1995) = 1997.50, rounded up to $2,000: the ask itself.
    assert _run(1995) == [("counter", 200_000)]
    state = NegotiationState(current_ask_cents=143_327, counters_used=1)  # an ask that is not a $5 amount
    decision = decide(state, 143_300, BALANCE, FLOOR)  # mid 1433.135 rounds up to $1,435, above the ask
    assert (decision.kind, decision.amount_cents) == ("counter", 143_327)


def test_state_is_returned_not_mutated() -> None:
    start = NegotiationState(current_ask_cents=BALANCE)
    decision = decide(start, 150_000, BALANCE, FLOOR)
    assert start == NegotiationState(BALANCE, 0)
    assert decision.state == NegotiationState(175_000, 1)
    after_two = NegotiationState(167_500, 2)
    assert decide(after_two, 139_999, BALANCE, FLOOR).state is after_two
