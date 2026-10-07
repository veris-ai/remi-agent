"""Remi's typed view of the bench's hand-off: which account, which phone, and when.

The bench stamps one call per pod as ``VERIS_AGENT_CONTEXT``: compact JSON
``{"account_id", "call_time", "phone_id"}``. It is the only task-specific fact
Remi receives, so it is parsed and validated once, at startup, and a missing
or malformed context fails the process loudly instead of producing a call that
silently talks about the wrong account.

Every date rule Remi applies is in Eastern time (New York City), so
``call_time`` is converted to Eastern here and the call's ``CallClock`` starts
from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from vamos_voice.context import AGENT_CONTEXT_ENV, AgentContext, ContextError, parse_instant

EASTERN = ZoneInfo("America/New_York")

_ACCOUNT_ID = re.compile(r"^TAS-\d{6}$")
_PHONE_ID = re.compile(r"^ph_[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class CallContext:
    """Which call this is: the account dialed, the phone dialed, and when it connected (Eastern)."""

    account_id: str
    phone_id: str
    call_time: datetime

    @classmethod
    def parse(cls, context: AgentContext) -> CallContext:
        """Remi's fields from the generic context. Raises ContextError on anything else."""
        if not context:
            raise ContextError(
                f"{AGENT_CONTEXT_ENV} is not set; Remi cannot know which account it is calling"
            )
        account_id, phone_id, call_time = context.require("account_id", "phone_id", "call_time")
        if not _ACCOUNT_ID.match(account_id):
            raise ContextError(f"account_id {account_id!r} is not TAS-NNNNNN")
        if not _PHONE_ID.match(phone_id):
            raise ContextError(f"phone_id {phone_id!r} is not a phones row id")
        when = parse_instant(call_time, "call_time")
        return cls(account_id=account_id, phone_id=phone_id, call_time=when.astimezone(EASTERN))

    @classmethod
    def from_json(cls, raw: str | None) -> CallContext:
        """Parse the raw ``VERIS_AGENT_CONTEXT`` value. Raises ContextError."""
        return cls.parse(AgentContext.from_json(raw))

    @property
    def call_date(self) -> date:
        """The call's date in Eastern time: "today" for every date rule."""
        return self.call_time.date()
