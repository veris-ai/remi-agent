"""Who speaks first, and what happens before they do.

Every agent declares a ``CallStartPolicy``; every provider honours it the same
way through the shared ``CallStart`` in its ``CallRuntime``.

``callee_first`` (outbound: the agent placed the call, the person who answers
speaks first; Remi):

1. **Prime.** As soon as the vendor session is open, the provider makes the
   model run the policy's connect-time reads as real tool calls, before the
   callee is heard. They land in the vendor's own frames (or SDK spans), so
   the judge's trace shows the reads first, and the model's first spoken
   turn already knows what they returned. The caller's audio is held (not
   dropped) until priming finishes, so "Hello?" is never lost and never
   interrupts the priming turn.
2. **Hold the line.** No audio goes out until the callee's first utterance
   has been heard. Anything the model produces before that is dropped.

The vendor decides only how to force the reads (see each provider).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

#: How a priming turn is labelled where a vendor can only be prompted by a user-role turn (Gemini Live),
#: so neither the model nor the judge mistakes it for the person on the line.
NOTE_LABEL = "[Dialer note, not the person on the line]"


@dataclass(frozen=True)
class CallStartPolicy:
    """Who speaks first and which reads run before anyone does."""

    first_speaker: Literal["callee", "agent"]
    connect_reads: tuple[str, ...]
    prime_instruction: str

    @property
    def prime_note(self) -> str:
        """The priming instruction as a labelled user-role turn."""
        return f"{NOTE_LABEL} {self.prime_instruction}"


def callee_first(connect_reads: tuple[str, ...], prime_instruction: str) -> CallStartPolicy:
    """Outbound: run ``connect_reads`` silently, then wait for the callee to speak first."""
    if not connect_reads:
        raise ValueError("callee_first needs at least one connect-time read to prime")
    return CallStartPolicy("callee", connect_reads, prime_instruction)


class CallStart:
    """One call's start state: primed? has the callee spoken? may audio go out?"""

    def __init__(self, policy: CallStartPolicy) -> None:
        self.policy = policy
        self._primed = asyncio.Event()
        self._callee_spoke = asyncio.Event()

    @property
    def primed(self) -> bool:
        return self._primed.is_set()

    @property
    def callee_spoke(self) -> bool:
        return self._callee_spoke.is_set()

    def mark_primed(self) -> None:
        if not self._primed.is_set():
            logger.info("call start: connect reads done")
        self._primed.set()

    def mark_callee_spoke(self) -> None:
        if not self._callee_spoke.is_set():
            logger.info("call start: callee spoke")
        self._callee_spoke.set()

    async def wait_primed(self) -> None:
        await self._primed.wait()

    @property
    def audio_open(self) -> bool:
        """May the agent's audio reach the callee yet?"""
        if self.policy.first_speaker == "agent":
            return True
        return self.primed and self.callee_spoke
