"""``CallSession``: the one path every tool call takes, for every agent and vendor.

Providers never run a handler themselves; they call ``dispatch(name, args)``
and send back the dict it returns. ``dispatch`` never raises. Unknown tools,
calls after the call ended, gate refusals, argument validation errors,
``ToolError``s and unexpected faults all come back as ``{"error": ...}`` for
the model to handle on the call, and are logged with the tool's ``redact``
fields replaced, so card data never reaches a log line.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ..tools.registry import ToolError, ToolRegistry
from .gates import Gates

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"


class CallState(Protocol):
    """What the engine needs from an agent's per-call state object."""

    #: Set by the agent's own hang-up tool; providers close the line once it is True.
    ended: bool

    async def finalize(self) -> None:
        """The voice connection closed: write whatever the call must always leave behind."""
        ...


class CallSession:
    """One call's dispatcher: the agent's state, its tools, its gates."""

    def __init__(self, state: CallState, registry: ToolRegistry, gates: Gates) -> None:
        self.state = state
        self.registry = registry
        self.gates = gates
        self.tool_log: list[str] = []

    @property
    def ended(self) -> bool:
        return self.state.ended

    async def dispatch(self, name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tool call. Always returns a JSON-able dict; refusals are ``{"error": ...}``."""
        tool = self.registry.get(name)
        args_for_log = _redacted(raw_args or {}, tool.redact if tool else frozenset())
        if tool is None:
            logger.warning("tool %s does not exist (args %s)", name, args_for_log)
            return {"error": f"There is no tool named {name}."}
        self.tool_log.append(name)
        try:
            if self.state.ended:
                raise ToolError("The call has ended.")
            if (refusal := self.gates.refusal(self.state, tool)) is not None:
                raise ToolError(refusal)
            args = tool.parse(raw_args or {})
            result = await tool.handler(self.state, args)
        except ToolError as err:
            logger.info("tool %s refused: %s (args %s)", name, err, args_for_log)
            return {"error": str(err)}
        except Exception:
            # Fail loud in the logs, but keep the call alive: the model can apologise and recover.
            logger.exception("tool %s failed (args %s)", name, args_for_log)
            return {"error": f"{name} failed because of a system problem. Do not retry it more than once."}
        logger.info("tool %s ok (args %s)", name, args_for_log)
        return result

    async def finalize(self) -> None:
        await self.state.finalize()


def _redacted(args: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {k: (REDACTED if k in fields else v) for k, v in args.items()}
