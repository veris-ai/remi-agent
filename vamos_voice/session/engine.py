"""``CallSession``: the one path every tool call takes, for every agent and vendor.

Providers never run a handler themselves; they call ``dispatch(name, args)``
and send back the dict it returns. ``dispatch`` never raises, except to pass on
the cancellation of the task awaiting it. Unknown tools, calls after the call
ended, gate refusals, argument validation errors, ``ToolError``s and unexpected
faults all come back as ``{"error": ...}`` for the model to handle on the call,
and are logged with the tool's ``redact`` fields replaced, so card data never
reaches a log line.

A tool call that passes those checks runs in its own task and always finishes:
a hang-up may cancel the provider loop awaiting ``dispatch`` (the loop gets the
cancellation) but not the call, so a charge that went through gets its record.

``finalize`` runs the agent's end-of-call writes exactly once, however many
paths ask for it. ``caller_gone`` starts it the moment the caller hangs up or
the socket drops, while the provider is still tearing its vendor session down:
a vendor socket's close handshake can take seconds, and the bench drains the
World's log right after the caller hangs up, so the agent's record must land
first. From then on no new tool call runs, and finalize waits for any tool
call already running, so the record is written after the call's last effect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from ..tools.registry import Tool, ToolError, ToolRegistry
from .gates import Gates

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"
#: How long finalize waits for tool calls already running (a database write, a charge) to settle.
SETTLE_TIMEOUT_S = 10.0


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
        #: The caller is gone (hung up or dropped): no new tool call runs.
        self.closing = False
        #: Tool calls started and not finished (held here: the event loop keeps only weak references).
        self._running: set[asyncio.Task[dict[str, Any]]] = set()
        self._settled = asyncio.Event()
        self._settled.set()
        self._finalizing: asyncio.Future[None] | None = None

    @property
    def ended(self) -> bool:
        return self.state.ended

    async def dispatch(self, name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
        """Run one tool call. Always returns a JSON-able dict; refusals are ``{"error": ...}``.

        Once the checks pass, the handler runs in its own task, awaited through ``asyncio.shield``:
        if the awaiting task is cancelled, the cancellation is re-raised to it and the tool call
        still runs to completion, and ``finalize`` waits for it."""
        tool = self.registry.get(name)
        args_for_log = _redacted(raw_args or {}, tool.redact if tool else frozenset())
        if tool is None:
            logger.warning("tool %s does not exist (args %s)", name, args_for_log)
            return {"error": f"There is no tool named {name}."}
        self.tool_log.append(name)
        try:
            if self.closing:
                raise ToolError("The caller has hung up; the call is over.")
            if self.state.ended:
                raise ToolError("The call has ended.")
            if (refusal := self.gates.refusal(self.state, tool)) is not None:
                raise ToolError(refusal)
            args = tool.parse(raw_args or {})
        except Exception as err:
            return _failure(name, err, args_for_log)
        call = asyncio.create_task(self._run(tool, args, args_for_log), name=f"tool {name}")
        self._running.add(call)
        self._settled.clear()
        call.add_done_callback(self._call_done)
        return await asyncio.shield(call)

    async def _run(self, tool: Tool, args: BaseModel, args_for_log: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await tool.handler(self.state, args)
        except Exception as err:
            return _failure(tool.name, err, args_for_log)
        logger.info("tool %s ok (args %s)", tool.name, args_for_log)
        return result

    def _call_done(self, call: asyncio.Task[dict[str, Any]]) -> None:
        self._running.discard(call)
        if not self._running:
            self._settled.set()

    def caller_gone(self) -> None:
        """The caller hung up or the line dropped: refuse new tool calls and start finalizing now,
        without waiting for the provider to finish tearing down its vendor session."""
        if not self.closing:
            logger.info("caller gone: finalizing the call now")
        self.closing = True
        self._start_finalizing()

    async def finalize(self) -> None:
        """The agent's end-of-call writes, exactly once; a later call waits for the first to finish."""
        await asyncio.shield(self._start_finalizing())

    def _start_finalizing(self) -> asyncio.Future[None]:
        if self._finalizing is None:
            self._finalizing = asyncio.ensure_future(self._finalize())
            self._finalizing.add_done_callback(_log_failure)
        return self._finalizing

    async def _finalize(self) -> None:
        try:
            await asyncio.wait_for(self._settled.wait(), SETTLE_TIMEOUT_S)
        except TimeoutError:
            logger.warning("finalizing with a tool call still running after %.0fs", SETTLE_TIMEOUT_S)
        await self.state.finalize()


def _log_failure(task: asyncio.Future[None]) -> None:
    """A finalize started by ``caller_gone`` may fail with nobody awaiting it yet: never silently."""
    if not task.cancelled() and (err := task.exception()) is not None:
        logger.error("the call could not be finalized", exc_info=err)


def _failure(name: str, err: Exception, args_for_log: dict[str, Any]) -> dict[str, Any]:
    """A refusal or a fault as ``{"error": ...}`` for the model, logged with the redacted arguments."""
    if isinstance(err, ToolError):
        logger.info("tool %s refused: %s (args %s)", name, err, args_for_log)
        return {"error": str(err)}
    # Fail loud in the logs, but keep the call alive: the model can apologise and recover.
    logger.error("tool %s failed (args %s)", name, args_for_log, exc_info=err)
    return {"error": f"{name} failed because of a system problem; whether it took effect is not known."}


def _redacted(args: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {k: (REDACTED if k in fields else v) for k, v in args.items()}
