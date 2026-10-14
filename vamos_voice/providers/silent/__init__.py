"""The silent baseline: a null agent that answers the call and then does nothing.

A benchmark sanity check, not a candidate: a grader must give it zero. It takes
the ``/voice`` connection exactly like every vendor (same server, framing,
screen pop and call start), listens to the caller until they hang up, and never
speaks, never calls a tool and never hangs up itself; the caller (the bench
actor) or the platform's timeout ends the call. No vendor, no key, no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from vamos_voice.providers.base import CallRuntime, VoiceProvider


class SilentProvider(VoiceProvider):
    """Holds the line in silence until the caller hangs up."""

    name = "silent"
    trace_adapter = None
    required_env = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        return cls()

    async def run_call(self, call: CallRuntime) -> None:
        async def discard(pcm: bytes) -> None:
            return None

        call.inbound.bind(discard)
        await call.inbound.run()  # returns when the caller hangs up or the socket drops


PROVIDER = SilentProvider
