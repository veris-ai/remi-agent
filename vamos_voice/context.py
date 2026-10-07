"""The bench's per-call hand-off, and the only clock an agent may use.

The bench may stamp each pod with ``VERIS_AGENT_CONTEXT``: a compact JSON
object of string values (for Remi: ``{"account_id", "call_time", "phone_id"}``).
The framework parses it once, at startup, into a generic ``AgentContext``: an
immutable string map. Which keys matter, and what they mean, is the agent's
business: each agent pack parses its own typed view from it (and fails loudly
when its keys are missing), so the framework never learns a domain field.

``CallClock`` is the World's clock. A bench World is frozen at a reference
instant that is not wall time (the debt-collection World is 2027-02-10), and
the databases' ``now()`` returns real wall time. So every timestamp an agent
writes and every date it computes comes from the reference instant plus the
monotonic time elapsed since the call connected, never from SQL or
``datetime.now()``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

AGENT_CONTEXT_ENV = "VERIS_AGENT_CONTEXT"


class ContextError(ValueError):
    """VERIS_AGENT_CONTEXT is malformed, or lacks what the agent needs."""


@dataclass(frozen=True)
class AgentContext(Mapping[str, str]):
    """The bench's hand-off as a string map. Empty when the bench sent none (inbound agents)."""

    values: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | None) -> AgentContext:
        """Parse the bench's compact JSON. Unset or empty means no context; anything malformed raises."""
        if not raw:
            return cls({})
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ContextError(f"{AGENT_CONTEXT_ENV} is not JSON: {err}") from err
        if not isinstance(payload, dict):
            raise ContextError(f"{AGENT_CONTEXT_ENV} must be a JSON object")
        not_strings = sorted(k for k, v in payload.items() if not isinstance(v, str))
        if not_strings:
            raise ContextError(f"{AGENT_CONTEXT_ENV} values must be strings; {not_strings} are not")
        return cls(dict(payload))

    def require(self, *keys: str) -> tuple[str, ...]:
        """The values of ``keys``, in order. Raises ContextError naming every missing key."""
        missing = [k for k in keys if k not in self.values]
        if missing:
            raise ContextError(f"{AGENT_CONTEXT_ENV} lacks {sorted(missing)}")
        return tuple(self.values[k] for k in keys)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def parse_instant(raw: str, what: str) -> datetime:
    """An ISO-8601 instant that carries its UTC offset. Raises ContextError otherwise."""
    try:
        when = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as err:
        raise ContextError(f"{what} {raw!r} is not ISO-8601") from err
    if when.tzinfo is None:
        raise ContextError(f"{what} {raw!r} carries no UTC offset")
    return when


@dataclass
class CallClock:
    """``call_time`` plus the monotonic time elapsed on the call. Never wall time.

    ``call_time`` is timezone-aware, in the zone the agent reasons in (the
    agent converts it before building the clock). ``start()`` marks the moment
    the call connected (the voice socket opened). Before that, and in tests,
    ``now()`` is ``call_time`` itself.
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
        """The World's current instant, in ``call_time``'s zone."""
        return self.call_time + self.elapsed

    def today(self) -> date:
        """The World's current date, in ``call_time``'s zone."""
        return self.now().date()
