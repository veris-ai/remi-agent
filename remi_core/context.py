"""The dialer's hand-off for this call, and the only clock Remi may use.

The bench stamps one call per pod as ``VERIS_AGENT_CONTEXT``: compact JSON
``{"account_id", "call_time", "phone_id"}``. It is the only task-specific fact
Remi receives, so it is parsed and validated once, at startup, and a missing
or malformed context fails the process loudly instead of producing a call that
silently talks about the wrong account.

``CallClock`` is the World's clock. The database's ``now()`` returns real wall
time (2026), not the World's frozen 2027-02-10, so every timestamp Remi writes
and every date it computes comes from ``call_time`` plus the monotonic time
elapsed since the call connected, never from SQL or ``datetime.now()``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

AGENT_CONTEXT_ENV = "VERIS_AGENT_CONTEXT"
EASTERN = ZoneInfo("America/New_York")

_ACCOUNT_ID = re.compile(r"^TAS-\d{6}$")
_PHONE_ID = re.compile(r"^ph_[A-Za-z0-9_]+$")


class ContextError(ValueError):
    """VERIS_AGENT_CONTEXT is missing or does not describe one call."""


@dataclass(frozen=True)
class CallContext:
    """Which call this is: the account dialed, the phone dialed, and when it connected."""

    account_id: str
    phone_id: str
    call_time: datetime

    @classmethod
    def from_json(cls, raw: str | None) -> CallContext:
        """Parse the bench's compact JSON. Raises ContextError on anything else."""
        if not raw:
            raise ContextError(
                f"{AGENT_CONTEXT_ENV} is not set; Remi cannot know which account it is calling"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ContextError(f"{AGENT_CONTEXT_ENV} is not JSON: {err}") from err
        if not isinstance(payload, dict):
            raise ContextError(f"{AGENT_CONTEXT_ENV} must be a JSON object")
        missing = {"account_id", "phone_id", "call_time"} - payload.keys()
        if missing:
            raise ContextError(f"{AGENT_CONTEXT_ENV} lacks {sorted(missing)}")
        account_id, phone_id, call_time = payload["account_id"], payload["phone_id"], payload["call_time"]
        if not isinstance(account_id, str) or not _ACCOUNT_ID.match(account_id):
            raise ContextError(f"account_id {account_id!r} is not TAS-NNNNNN")
        if not isinstance(phone_id, str) or not _PHONE_ID.match(phone_id):
            raise ContextError(f"phone_id {phone_id!r} is not a phones row id")
        try:
            when = datetime.fromisoformat(call_time)
        except (TypeError, ValueError) as err:
            raise ContextError(f"call_time {call_time!r} is not ISO-8601") from err
        if when.tzinfo is None:
            raise ContextError(f"call_time {call_time!r} carries no UTC offset")
        return cls(account_id=account_id, phone_id=phone_id, call_time=when.astimezone(EASTERN))

    @property
    def call_date(self) -> date:
        """The call's date in Eastern time: "today" for every date rule."""
        return self.call_time.date()


@dataclass
class CallClock:
    """``call_time`` plus the monotonic time elapsed on the call. Never wall time.

    ``start()`` marks the moment the call connected (the voice socket opened).
    Before that, and in tests, ``now()`` is ``call_time`` itself.
    """

    call_time: datetime
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _started_at: float | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        """The call connected; elapsed time counts from here."""
        if self._started_at is None:
            self._started_at = self.monotonic()

    @property
    def elapsed(self) -> timedelta:
        """Time on the call so far."""
        if self._started_at is None:
            return timedelta(0)
        return timedelta(seconds=max(0.0, self.monotonic() - self._started_at))

    def now(self) -> datetime:
        """The World's current instant, Eastern."""
        return self.call_time + self.elapsed

    def today(self) -> date:
        """The World's current date, Eastern."""
        return self.now().date()
