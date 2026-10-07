"""Gates: which tool may run now, decided in code rather than hoped for in the prompt.

An agent hands the engine one ``Gates`` object. ``refusal(state, tool)``
returns None when the tool may run, or a message telling the model what must
happen first (it becomes ``{"error": message}``; the call goes on).

``GateLadder`` is the common case: an ordered state machine. The agent
defines its levels as an ``IntEnum`` (Remi: connect reads, assessed,
proceeding, verified, debt unlocked; an inbound agent: unverified, verified),
says how to read the current level off its state, and how to explain a
refusal. A tool declaring ``requires=Level.X`` runs only once the call has
reached ``X``; a tool declaring nothing is ungated.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..tools.registry import Tool


class Gates(Protocol):
    """Decides whether one tool call may run against the call's state."""

    def refusal(self, state: Any, tool: Tool) -> str | None:
        """None to allow; otherwise what the model must do first."""
        ...


@dataclass(frozen=True)
class GateLadder[S]:
    """Ordered gate levels: ``tool.requires`` is the least level the call must have reached."""

    level: Callable[[S], enum.IntEnum]
    explain: Callable[[S, enum.IntEnum], str]

    def refusal(self, state: S, tool: Tool) -> str | None:
        if tool.requires is None or self.level(state) >= tool.requires:
            return None
        return self.explain(state, tool.requires)
