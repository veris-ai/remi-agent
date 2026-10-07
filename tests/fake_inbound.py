"""A tiny INBOUND agent, for tests only (not a pack: it is never discoverable or shipped).

It has the shape of an inbound customer-service agent such as Rory (energy) or
Riley (banking): the caller rings, the agent speaks first, nothing is read
before the greeting, and account tools stay locked until the caller passes a
verification tool. It proves that ``AgentSpec`` and every provider support
``AGENT_FIRST`` without a framework change.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field

from vamos_voice.agent import AgentSpec
from vamos_voice.call_start import agent_first
from vamos_voice.context import AgentContext, CallClock, parse_instant
from vamos_voice.session import GateLadder
from vamos_voice.tools import ToolError, ToolRegistry

GREETING = "Thanks for calling Acme Energy, this is Ivy. How can I help you today?"
REFERENCE_TIME_ENV = "IVY_REFERENCE_TIME"
DEFAULT_REFERENCE_TIME = "2027-03-01T10:00:00+00:00"


class Level(enum.IntEnum):
    CALLER = 0
    VERIFIED = 1


class IvyCall:
    """Per-call state: who the caller proved to be."""

    def __init__(self, clock: CallClock) -> None:
        self.clock = clock
        self.account: str | None = None
        self.ended = False
        self.finalized = False

    async def finalize(self) -> None:
        self.finalized = True


class VerifyArgs(BaseModel):
    account_number: str = Field(description="The account number the caller read out.")
    postal_code: str = Field(description="The caller's postal code.")


class NoArgs(BaseModel):
    pass


TOOLS = ToolRegistry()


@TOOLS.tool(VerifyArgs, redact=("postal_code",))
async def verify_caller(call: IvyCall, args: VerifyArgs) -> dict[str, Any]:
    """Verify the caller with their account number and postal code."""
    if (args.account_number, args.postal_code) != ("ACME-1", "10001"):
        raise ToolError("Those details do not match an account.")
    call.account = args.account_number
    return {"verified": True}


@TOOLS.tool(NoArgs, requires=Level.VERIFIED)
async def get_account(call: IvyCall, _: NoArgs) -> dict[str, Any]:
    """Read the verified caller's account."""
    return {"account": call.account, "as_of": call.clock.now().isoformat()}


@TOOLS.tool(NoArgs)
async def end_call(call: IvyCall, _: NoArgs) -> dict[str, Any]:
    """Hang up after saying goodbye."""
    call.ended = True
    return {"ended": True}


class FakeInboundAgent(AgentSpec):
    name = "fake_inbound"
    call_start = agent_first(f'Greet the caller now with exactly: "{GREETING}"')
    tools = TOOLS
    gates = GateLadder[IvyCall](
        level=lambda call: Level.VERIFIED if call.account else Level.CALLER,
        explain=lambda call, needed: "Verify the caller first with verify_caller.",
    )

    def __init__(self, reference_time: datetime) -> None:
        self.reference_time = reference_time

    @classmethod
    def from_env(cls, env: Mapping[str, str], context: AgentContext) -> Self:
        # Inbound: no per-call context; the World's instant comes from the agent's own env.
        raw = env.get(REFERENCE_TIME_ENV) or DEFAULT_REFERENCE_TIME
        return cls(parse_instant(raw, REFERENCE_TIME_ENV))

    def call_time(self) -> datetime:
        return self.reference_time

    def instructions(self, clock: CallClock) -> str:
        return (
            "You are Ivy, the inbound voice assistant for Acme Energy. "
            f"Today is {clock.today():%A, %B %d, %Y}. Verify callers before discussing an account."
        )

    def open_call(self, clock: CallClock) -> IvyCall:
        return IvyCall(clock)
