"""Who speaks first, and what happens before they do.

On a real outbound call the person who answers speaks first; the bench's actor
does the same (WORLD.md, "Who speaks first"). Remi's policy, ``CALLEE_FIRST``:

1. **Prime.** As soon as the vendor session is open, the provider makes the
   model run the three connect-time reads as real tool calls, before the
   callee is heard. They land in the vendor's own frames (or SDK spans), so
   the judge's trace shows the reads first, and the model's first spoken
   turn already knows whether the call may proceed. The caller's audio is
   held (not dropped) until priming finishes, so "Hello?" is never lost and
   never interrupts the priming turn.
2. **Hold the line.** No audio goes out until the callee's first utterance
   has been heard. Anything the model produces before that is dropped.

Every provider gets the same ``CallStart`` from the shared runtime; the
vendor decides only how to force the reads (see each provider).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from .session import CONNECT_READS

logger = logging.getLogger(__name__)

PRIME_INSTRUCTION = (
    "The call has just connected and nobody has spoken yet. Run the connect-time checks now by calling "
    "get_account, get_dialed_phone and get_contact_history. Do not say anything."
)
#: Text the priming turn carries where a vendor can only be prompted by a user-role turn (Gemini Live).
PRIME_NOTE = f"[Dialer note, not the person on the line] {PRIME_INSTRUCTION}"


@dataclass(frozen=True)
class CallStartPolicy:
    """Who speaks first and which reads run before anyone does."""

    first_speaker: Literal["callee", "agent"]
    connect_reads: tuple[str, ...]
    prime_instruction: str


CALLEE_FIRST = CallStartPolicy(
    first_speaker="callee", connect_reads=CONNECT_READS, prime_instruction=PRIME_INSTRUCTION
)


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
