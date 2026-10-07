"""Agent discovery: an agent is a package ``vamos_agents.<name>`` exporting an ``AgentSpec`` as ``AGENT``.

The framework never imports an agent by name in code; it resolves
``VAMOS_AGENT`` at runtime by convention, the same way vendors are found
under ``vamos_voice/providers/``. Adding an agent means adding
``vamos_agents/<name>/`` and a ``<name>`` extra in pyproject.toml, nothing here.
An image carries exactly one agent folder, so only that one is importable.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil

from .agent import AgentSpec

#: The package agent packs live under.
AGENTS_PACKAGE = "vamos_agents"


class UnknownAgent(LookupError):
    """VAMOS_AGENT names no agent pack, or the pack does not export a valid AGENT."""


def agent_names() -> list[str]:
    """Every agent pack present in this checkout or image, installed extras or not."""
    spec = importlib.util.find_spec(AGENTS_PACKAGE)
    if spec is None or not spec.submodule_search_locations:
        return []
    return sorted(m.name for m in pkgutil.iter_modules(spec.submodule_search_locations) if m.ispkg)


def load_agent(name: str) -> type[AgentSpec]:
    """Import ``vamos_agents.<name>`` and return its ``AGENT``. Import errors propagate:
    a pack whose extra is not installed must fail loudly, not vanish."""
    if name not in agent_names():
        raise UnknownAgent(f"no agent {name!r}; known: {', '.join(agent_names()) or 'none'}")
    module = importlib.import_module(f"{AGENTS_PACKAGE}.{name}")
    agent = getattr(module, "AGENT", None)
    if not (isinstance(agent, type) and issubclass(agent, AgentSpec)):
        raise UnknownAgent(f"{AGENTS_PACKAGE}.{name} does not export an AgentSpec as AGENT")
    if agent.name != name:
        raise UnknownAgent(f"{AGENTS_PACKAGE}.{name}.AGENT.name is {agent.name!r}; it must match the folder")
    return agent
