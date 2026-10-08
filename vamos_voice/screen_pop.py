"""The screen pop: what the agent's screen shows the moment a call connects.

A real contact-centre dialer pops the account (or the caller's record) on the
agent's screen when the line connects; the agent does not have to look it up
while the person says "Hello?". An agent declares its screen pop with
``AgentSpec.screen_pop(call)``: it performs its connect-time reads itself,
through its own world clients and the same handlers its tools use, records
them on the call state (so its gates treat them as done), and returns a
``ScreenPop`` whose text the framework appends to the session instructions.

The server runs it once per call, after the call state exists and before any
provider opens its vendor session, so the reads always precede any caller
audio reaching a vendor and any agent audio reaching the caller, and every
vendor receives the same instructions. No model is asked to make the reads,
so no vendor's tool-choice support (or lack of it) changes what the agent
knows when the callee speaks.

A screen pop that fails is logged loudly and the call goes on without it: the
agent's tools and gates still let the model make the reads itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import AgentSpec
    from .session import CallState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenPop:
    """A labelled section of call context, rendered by the agent from its connect-time reads."""

    heading: str
    body: str

    def render(self) -> str:
        return f"# {self.heading}\n\n{self.body.strip()}"


def with_screen_pop(instructions: str, pop: ScreenPop | None) -> str:
    """The session instructions with the screen pop appended as its own section."""
    return f"{instructions.rstrip()}\n\n{pop.render()}\n" if pop else instructions


async def run_screen_pop(agent: AgentSpec, call: CallState) -> ScreenPop | None:
    """The agent's screen pop for this call, or None (no screen pop, or it failed: logged)."""
    try:
        pop = await agent.screen_pop(call)
    except Exception:
        logger.exception("screen pop failed for %s; the call starts without it", agent.name)
        return None
    if pop is not None:
        logger.info("screen pop: %s (%d chars)", pop.heading, len(pop.body))
    return pop
