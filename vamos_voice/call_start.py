"""Who speaks first.

Every agent declares a ``CallStartPolicy``; every provider honours it the same
way through the shared ``CallStart`` in its ``CallRuntime``.

``callee_first`` (outbound: the agent placed the call, the person who answers
speaks first; Remi): no audio goes out until the callee's first utterance has
been heard. Anything the model produces before that is dropped.

``agent_first`` (inbound: the caller rang the agent, the agent greets them;
Rory, Riley): the provider asks the model for its opening turn with the
policy's ``opening_instruction`` (a greeting), and agent audio flows from the
first byte: the line is never held.

What the agent knows when the call connects is not a call-start concern: it
is the agent's screen pop (``vamos_voice.screen_pop``), run by the server
before any provider opens its vendor session, the same for every vendor.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

#: How an opening turn is labelled where a vendor can only be prompted by a user-role turn
#: (Gemini Live), so neither the model nor the judge mistakes it for the person on the line.
NOTE_LABEL = "[Call system note, not the person on the line]"


@dataclass(frozen=True)
class CallStartPolicy:
    """Who speaks first, and (agent first) what the opening turn should do."""

    first_speaker: Literal["callee", "agent"]
    #: agent_first only: what the model is told to do for its opening turn (greet the caller).
    opening_instruction: str = ""

    @property
    def opening_note(self) -> str:
        """The opening instruction as a labelled user-role turn."""
        return f"{NOTE_LABEL} {self.opening_instruction}"

    @property
    def agent_opens(self) -> bool:
        """Does the agent speak first (so the provider must request an opening turn)?"""
        return self.first_speaker == "agent"


def callee_first() -> CallStartPolicy:
    """Outbound: wait for the callee to speak first."""
    return CallStartPolicy("callee")


def agent_first(opening_instruction: str) -> CallStartPolicy:
    """Inbound: the agent opens the call with ``opening_instruction``."""
    if not opening_instruction:
        raise ValueError("agent_first needs an opening instruction (what the greeting turn should do)")
    return CallStartPolicy("agent", opening_instruction)


class CallStart:
    """One call's start state: has the callee spoken? may agent audio go out?"""

    def __init__(self, policy: CallStartPolicy) -> None:
        self.policy = policy
        self._callee_spoke = asyncio.Event()

    @property
    def callee_spoke(self) -> bool:
        return self._callee_spoke.is_set()

    def mark_callee_spoke(self) -> None:
        if not self._callee_spoke.is_set():
            logger.info("call start: callee spoke")
        self._callee_spoke.set()

    @property
    def audio_open(self) -> bool:
        """May the agent's audio reach the callee yet?"""
        return self.policy.agent_opens or self.callee_spoke
