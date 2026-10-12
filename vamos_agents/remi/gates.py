"""Remi's gates: RULES.md's opening order as a ladder, plus the collection stop.

``Gate`` is the least a tool needs before it may run. ``GATES`` refuses a tool
whose level the call has not reached (``RemiCall.refusal_for`` says what must
happen first), and refuses every tool tagged ``COLLECTION`` once something on
the call (a dispute, cease, attorney, bankruptcy, death, SCRA) stopped
collection, or once the call was transferred to a person. Once SCRA is recorded the transfer is mandatory (RULES.md, "Active
military duty"), so the tools tagged ``CLOSING`` wait for ``transfer_to_human``.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from vamos_voice.session import GateLadder
from vamos_voice.tools import Tool

if TYPE_CHECKING:
    from .call import RemiCall

#: Tag on tools that state an amount, make an offer or take money.
COLLECTION = "collection"
#: Tag on the tools that close the call (log it, hang up): they wait for steps a rule makes mandatory.
CLOSING = "closing"


class Gate(enum.IntEnum):
    """RULES.md's opening order, as the least a tool needs before it may run."""

    NONE = 0  # the connect-time reads themselves
    ASSESSED = 1  # all three reads done; the call may proceed or must close
    PROCEEDING = 2  # assessed, and the call is not one that should not have been placed
    VERIFIED = 3  # the right party (consumer or verified estate rep) confirmed
    DEBT_UNLOCKED = 4  # recording disclosure, mini-Miranda and the rest given; debt details read


class RemiGates:
    """The opening-order ladder, then the collection stop, then the steps a closing must wait for."""

    def __init__(self) -> None:
        self._ladder: GateLadder[RemiCall] = GateLadder(
            level=lambda call: call.gate,
            explain=lambda call, needed: call.refusal_for(Gate(needed)),
        )

    def refusal(self, call: RemiCall, tool: Tool) -> str | None:
        if (refused := self._ladder.refusal(call, tool)) is not None:
            return refused
        if COLLECTION in tool.tags and call.collection_stopped_by:
            return (
                f"Collection stopped on this call ({call.collection_stopped_by.replace('_', ' ')}). "
                "No amount, offer or payment request is allowed for the rest of the call."
            )
        if COLLECTION in tool.tags and call.transferred:
            return (
                "The call was transferred to a person, who takes it from here: no amount, offer or payment is "
                "allowed on this call any more. Next: log_contact_attempt and end_call."
            )
        if CLOSING in tool.tags and call.scra_recorded and not call.transferred:
            return (
                "Active military duty was recorded: the call goes to a person before it is logged or "
                "ended (transfer_to_human, reason scra)."
            )
        return None


GATES = RemiGates()
