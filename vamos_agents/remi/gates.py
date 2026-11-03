"""Remi's gates: none. No tool is refused, blocked or steered for policy.

The bench measures the agent, so an agent's mistake must show as the agent's
failure (World v2 WORLD.md, "Nothing refuses the agent"): a payment before
verification, an offer after a dispute or a call closed without a transfer is
the agent's to make, and the bench grades it from what the agent said and what
it recorded. Remi still supplies the framework's ``Gates``, so every tool call
goes through ``CallSession.dispatch`` as for any agent; this one refuses nothing.
"""

from __future__ import annotations

from typing import Any

from vamos_voice.tools import Tool


class RemiGates:
    """The framework's ``Gates``, refusing nothing."""

    def refusal(self, call: Any, tool: Tool) -> str | None:
        return None


GATES = RemiGates()
