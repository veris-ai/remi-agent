"""``AgentSpec``: everything that makes one agent that agent, and nothing about voice vendors.

An agent pack (``agents/<name>/``) subclasses ``AgentSpec`` and exports the
class as ``AGENT``. The framework finds it by name (``vamos_voice.registry``),
validates it at startup, and drives every call through it; no framework file
changes when an agent is added.

What an agent declares (class attributes):

- ``name``: the folder under ``agents/``, the ``AGENT`` build arg, the extra in
  pyproject.toml and the first half of the image name.
- ``call_start``: who speaks first and which connect-time reads run before
  (``vamos_voice.call_start``: ``callee_first`` for outbound, ``agent_first``
  for inbound).
- ``tools``: its ``ToolRegistry``.
- ``gates``: its ``Gates`` (usually a ``GateLadder``) deciding what may run now.
- ``required_env``: variables it cannot start without (checked before ``from_env``).

What an agent implements:

- ``from_env(env, context)``: parse its typed view of the bench context and
  its config, and build its world-client factories (database, payments,
  core-banking...) without doing I/O. Raise ``ValueError`` (``ConfigError``)
  naming what is wrong; the process exits before the port opens.
- ``call_time()``: the reference instant the call's ``CallClock`` starts from.
- ``connect()`` / ``close()``: open and close the world clients; ``/health``
  stays 503 until ``connect()`` returns.
- ``instructions(clock)``: the system prompt for this call, rendered from the
  context and the clock only, so every vendor sends identical text.
- ``open_call(clock)``: a fresh per-call state object (``CallState``); every
  tool handler receives it.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar, Self

from .call_start import CallStartPolicy
from .context import AgentContext, CallClock
from .session import CallState, Gates
from .tools import ToolRegistry


class ConfigError(ValueError):
    """The environment cannot run a call; the message names what is missing."""


class AgentSpec(abc.ABC):
    """One VAmoS agent: its call start, prompt, tools, gates and world clients."""

    name: ClassVar[str]
    call_start: ClassVar[CallStartPolicy]
    tools: ClassVar[ToolRegistry]
    gates: ClassVar[Gates]
    required_env: ClassVar[tuple[str, ...]] = ()

    @classmethod
    @abc.abstractmethod
    def from_env(cls, env: Mapping[str, str], context: AgentContext) -> Self:
        """Validate config and context, build world-client factories. No I/O. Raises ValueError."""

    @abc.abstractmethod
    def call_time(self) -> datetime:
        """The World's instant when the call connects (timezone-aware): the clock's origin."""

    async def connect(self) -> None:  # noqa: B027 - optional hook, deliberately empty
        """Open world clients (retrying while twins start). /health is 503 until this returns."""

    async def close(self) -> None:  # noqa: B027 - optional hook, deliberately empty
        """Close world clients at shutdown."""

    @abc.abstractmethod
    def instructions(self, clock: CallClock) -> str:
        """The system instructions for one call, from the context and clock only."""

    @abc.abstractmethod
    def open_call(self, clock: CallClock) -> CallState:
        """A fresh per-call state; tool handlers receive it, ``finalize()`` runs at hang-up."""

    def describe(self) -> str:
        """One log-line summary of which call this pod serves (no secrets)."""
        return self.name


def missing_agent_env(agent: type[AgentSpec], env: Mapping[str, str]) -> list[str]:
    """The agent's ``required_env`` keys that are unset or empty."""
    return [key for key in agent.required_env if not env.get(key)]
